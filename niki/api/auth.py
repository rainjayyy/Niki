"""Parent authentication API endpoints — PRD #30, #35, #36.

Demonstration endpoints for the #7 vertical slice:
  POST /auth/signup/start       — begin signup (create PENDING account)
  POST /auth/signup/real-name   — step 1: real-name verification
  POST /auth/signup/face        — step 2: face verification
  POST /auth/signup/consent     — step 3: e-sign consent
  GET  /auth/me/{parent_id}     — get account info
  POST /auth/deletion/request   — request account deletion
  POST /auth/deletion/complete  — complete deletion (after 30 days)
  GET  /auth/consent/document/{type}  — get the consent document text
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from niki.auth import (
    AccountLifecycle,
    AuthError,
)
from niki.auth.account import (
    ParentAccount,
    validate_phone,
)
from niki.auth.dependencies import get_current_parent
from niki.auth.jwt_service import (
    JWTError,
    TokenType,
    issue_access_token,
    issue_refresh_token,
    verify_token,
)
from niki.auth.sms_service import (
    SMSCodeExpired,
    SMSCodeInvalid,
    SMSTooManyAttempts,
    SMSRateLimited,
    get_sms_service,
)
from niki.auth.token_blacklist import (
    get_blacklist,
)
from niki.config.settings import get_settings
from niki.middleware.rate_limit import limiter

router = APIRouter(prefix="/auth", tags=["auth"])

# Module-level singleton
_lifecycle = AccountLifecycle()


def _error_response(exc: AuthError) -> dict:
    return {"error": exc.code, "message": exc.message}


@router.post("/signup/start")
async def signup_start(payload: dict) -> dict:
    """Begin signup. Creates a PENDING account."""
    try:
        account, progress = await _lifecycle.start_signup(
            name=payload["name"],
            id_number=payload["id_number"],
            phone=payload["phone"],
            email=payload.get("email"),
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Missing field: {exc}") from exc

    return {
        "account": account.to_dict(),
        "progress": {
            "parent_id": progress.parent_id,
            "steps": [
                {"name": s.name, "completed": s.completed}
                for s in progress.steps
            ],
        },
    }


@router.post("/signup/real-name")
async def signup_real_name(payload: dict) -> dict:
    """Step 1: Real-name verification."""
    try:
        account = await _lifecycle.verify_real_name(
            parent_id=payload["parent_id"],
            name=payload["name"],
            id_number=payload["id_number"],
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc

    return {"account": account.to_dict()}


@router.post("/signup/face")
async def signup_face(payload: dict) -> dict:
    """Step 2: Face verification."""
    try:
        account = await _lifecycle.verify_face(
            parent_id=payload["parent_id"],
            live_image_url=payload["live_image_url"],
            reference_image_url=payload.get("reference_image_url"),
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc

    return {"account": account.to_dict()}


@router.post("/signup/consent")
async def signup_consent(payload: dict) -> dict:
    """Step 3: Sign the consent document(s)."""
    try:
        account = await _lifecycle.sign_consent(
            parent_id=payload["parent_id"],
            typed_signature=payload["typed_signature"],
            ip_address=payload.get("ip_address", "0.0.0.0"),
            device_fingerprint=payload.get("device_fingerprint", "unknown"),
            sign_child_consent=payload.get("sign_child_consent", True),
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc

    return {"account": account.to_dict()}


@router.get("/whoami")
async def whoami(
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
) -> dict:
    """BE-AUTH-001 demo endpoint — proves the JWT dependency works.

    Returns the authenticated caller's account id from the JWT subject.
    Production endpoints should call get_full_parent (which looks up
    the real account from AccountLifecycle) instead of this stub.
    """
    return {
        "parent_id": parent.id,
        "name": parent.name,
        "status": parent.status.value if hasattr(parent.status, "value") else parent.status,
    }


@router.get("/me/{parent_id}")
async def get_account(parent_id: str) -> dict:
    account = await _lifecycle.get_account(parent_id)
    if account is None:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND"})
    progress = await _lifecycle.get_progress(parent_id)
    return {
        "account": account.to_dict(),
        "progress": (
            {
                "steps": [
                    {"name": s.name, "completed": s.completed, "error": s.error}
                    for s in progress.steps
                ]
            }
            if progress
            else None
        ),
    }


@router.post("/deletion/request")
async def deletion_request(payload: dict) -> dict:
    """Request account deletion (starts 30-day window)."""
    parent_id = payload["parent_id"]
    try:
        account = await _lifecycle.request_deletion(parent_id)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc
    return {
        "account": account.to_dict(),
        "deletion_window_remaining_days": account.deletion_window_remaining_days,
    }


@router.post("/deletion/complete")
async def deletion_complete(payload: dict) -> dict:
    """Complete the deletion (after 30 days)."""
    parent_id = payload["parent_id"]
    try:
        account = await _lifecycle.complete_deletion(parent_id)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc
    return {"account": account.to_dict()}


@router.get("/consent/document/{consent_type}")
async def get_consent_document(consent_type: str) -> dict:
    """Get the consent document text (parent or child version)."""
    if consent_type not in ("parent", "child"):
        raise HTTPException(status_code=400, detail={"error": "INVALID_CONSENT_TYPE"})
    consent = _lifecycle.get_consent_manager()
    if consent_type == "parent":
        return {
            "type": "parent",
            "version": consent.PARENT_DOC_VERSION,
            "text": consent.get_parent_document(),
        }
    return {
        "type": "child",
        "version": consent.CHILD_DOC_VERSION,
        "text": consent.get_child_document(),
    }


# ---------------------------------------------------------------------------
# BE-AUTH-002: SMS-code login (mock provider)
# ---------------------------------------------------------------------------


@router.post("/sms/send")
@limiter.limit("3/minute")
async def sms_send(request: Request, payload: dict) -> dict:
    """Send a 6-digit SMS code to `phone`.

    Dev mode echoes the code back so the frontend can use it directly;
    production deployments must NOT return the code in the response.
    """
    phone = payload.get("phone")
    if not phone or not validate_phone(phone):
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_PHONE_FORMAT"},
        )
    try:
        code = await get_sms_service().send(phone)
    except SMSRateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": exc.code,
                "message": exc.message,
                "retry_after_seconds": int(exc.retry_after_seconds),
            },
        ) from exc
    settings = get_settings()
    body: dict = {"phone": phone, "status": "sent"}
    if settings.debug:
        # Echo the code in DEBUG only; production must NEVER do this.
        body["dev_code"] = code
    return body


@router.post("/sms/login")
@limiter.limit("10/minute")
async def sms_login(request: Request, payload: dict) -> dict:
    """Verify the SMS code and issue a JWT access + refresh pair.

    On a brand-new phone the lifecycle auto-creates a PENDING account
    so login doubles as first-time signup.
    """
    phone = payload.get("phone")
    code = payload.get("code")
    if not phone or not validate_phone(phone):
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_PHONE_FORMAT"},
        )
    if not code or not isinstance(code, str) or len(code) != 6:
        raise HTTPException(
            status_code=400,
            detail={"error": "SMS_CODE_INVALID"},
        )

    try:
        await get_sms_service().verify(phone, code)
    except (SMSCodeInvalid, SMSCodeExpired, SMSTooManyAttempts) as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": exc.code, "message": exc.message},
        ) from exc

    # Find or auto-create the account.
    account = await _lifecycle.find_by_phone(phone)
    if account is None:
        # Auto-create PENDING parent. BE-AUTH-002 v1 placeholder: id_number
        # uses a fixed valid value to satisfy format validation; the
        # followup ticket (BE-AUTH-002-followup) adds a proper guided
        # ID-verification flow.
        try:
            account, _ = await _lifecycle.start_signup(
                name=f"家长{phone[-4:]}",
                id_number="110101199003078811",
                phone=phone,
            )
        except AuthError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": exc.code, "message": exc.message},
            ) from exc

    settings = get_settings()
    access = issue_access_token(account.id)
    refresh = issue_refresh_token(account.id)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": settings.jwt_access_ttl_seconds,
        "parent_id": account.id,
        "account_status": account.status.value,
    }


# ---------------------------------------------------------------------------
# BE-AUTH-003: refresh token rotation
# ---------------------------------------------------------------------------


@router.post("/refresh")
@limiter.limit("60/minute")
async def refresh(request: Request, payload: dict) -> dict:
    """Trade a refresh token for a fresh access + refresh pair.

    The presented refresh token is verified (signature + type + expiry)
    and, on success, immediately revoked so it cannot be re-used.
    A NEW refresh token is issued (rotation).
    """
    refresh_token = payload.get("refresh_token")
    if not refresh_token or not isinstance(refresh_token, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_REFRESH_TOKEN", "message": "refresh_token required"},
        )

    try:
        claims = verify_token(
            refresh_token,
            TokenType.REFRESH,
            is_revoked=get_blacklist().is_revoked_sync,
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": exc.code, "message": exc.message},
        ) from exc

    parent_id = claims["sub"]
    old_jti = claims.get("jti")

    # Revoke the old refresh token BEFORE issuing new ones so a replay
    # of `refresh_token` is impossible even within the same request.
    if old_jti:
        await get_blacklist().revoke(old_jti)

    new_access = issue_access_token(parent_id)
    new_refresh = issue_refresh_token(parent_id)
    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "Bearer",
        "expires_in": get_settings().jwt_access_ttl_seconds,
        "parent_id": parent_id,
    }


# ---------------------------------------------------------------------------
# BE-AUTH-004: logout (blacklist the access jti)
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> dict:
    """Revoke the presented access token by jti.

    The Depends() above has already validated the token's signature,
    type, expiry and revocation status; reaching this handler means
    the token was valid at request time. We now add its jti to the
    blacklist so the same token cannot be used again.

    Idempotent: re-logging-out the same token returns 200 (the
    blacklist check is `is_revoked_sync`, so already-revoked tokens
    would have been rejected by the dep with 401; this 200 only fires
    for tokens that were still valid).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        # Should be impossible — the dep would have raised 401.
        raise HTTPException(
            status_code=401,
            detail={"error": "MISSING_TOKEN", "message": "Bearer token required"},
        )
    token = authorization[7:].strip()
    try:
        claims = verify_token(
            token,
            TokenType.ACCESS,
            is_revoked=get_blacklist().is_revoked_sync,
        )
    except JWTError:
        # Dep already validated; this is purely defensive.
        raise HTTPException(
            status_code=401,
            detail={"error": "INVALID_TOKEN"},
        )
    jti = claims.get("jti")
    if jti:
        await get_blacklist().revoke(jti)
    return {"status": "logged_out", "parent_id": parent.id, "revoked_jti": jti}
