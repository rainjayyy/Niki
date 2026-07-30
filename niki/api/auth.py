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
from niki.auth.captcha import get_captcha_service
from niki.auth.token_blacklist import (
    get_blacklist,
)
from niki.config.settings import get_settings
from niki.middleware.rate_limit import limiter

from niki.api._i18n import localized_error_response, resolve_lang
from niki.i18n.translator import get_translator

router = APIRouter(prefix="/auth", tags=["auth"])

# Module-level singleton
_lifecycle = AccountLifecycle()

# Translation-key map for the structured AuthError codes. The
# `code` is what the frontend branches on (and what we test
# against); the `key` is dotted and resolved by the i18n framework
# per the request's Accept-Language.
_AUTH_KEY_BY_CODE: dict[str, str] = {
    "INVALID_PHONE_FORMAT": "auth.invalid_phone_format",
    "TOKEN_EXPIRED": "auth.token_expired",
    "TOKEN_INVALID": "auth.token_invalid",
    "TOKEN_REVOKED": "auth.token_revoked",
    "MISSING_TOKEN": "auth.missing_token",
    "INVALID_REFRESH_TOKEN": "auth.invalid_refresh_token",
    "WRONG_TOKEN_TYPE": "auth.wrong_token_type",
    "SMS_RATE_LIMITED": "auth.sms_rate_limited",
    "SMS_CODE_INVALID": "auth.sms_code_invalid",
    "SMS_CODE_EXPIRED": "auth.sms_code_expired",
    "SMS_TOO_MANY_ATTEMPTS": "auth.sms_too_many_attempts",
    "NOT_FOUND": "auth.not_found",
}


def _error_response(request: Request, exc: AuthError) -> dict:
    """Render an AuthError using the localized `message` field.

    Preserves the §6.3 contract: `error` is the machine-readable
    CODE, `message` is rendered through the i18n catalog using the
    request's resolved language. The exception's own `.message`
    (English, set by `AuthError.__init__`) is used as the fallback
    when no catalog entry exists yet for the code.
    """
    key = _AUTH_KEY_BY_CODE.get(exc.code)
    if key is not None:
        return localized_error_response(
            request,
            code=exc.code,
            key=key,
            fallback_message=exc.message,
        )
    # Unknown code → keep the exception's literal message so we
    # never regress existing error text.
    return {"error": exc.code, "message": exc.message}


@router.post("/signup/start")
async def signup_start(request: Request, payload: dict) -> dict:
    """Begin signup. Creates a PENDING account."""
    try:
        account, progress = await _lifecycle.start_signup(
            name=payload["name"],
            id_number=payload["id_number"],
            phone=payload["phone"],
            email=payload.get("email"),
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_response(request, exc),
        ) from exc
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
async def signup_real_name(request: Request, payload: dict) -> dict:
    """Step 1: Real-name verification."""
    try:
        account = await _lifecycle.verify_real_name(
            parent_id=payload["parent_id"],
            name=payload["name"],
            id_number=payload["id_number"],
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_response(request, exc),
        ) from exc

    return {"account": account.to_dict()}


@router.post("/signup/face")
async def signup_face(request: Request, payload: dict) -> dict:
    """Step 2: Face verification."""
    try:
        account = await _lifecycle.verify_face(
            parent_id=payload["parent_id"],
            live_image_url=payload["live_image_url"],
            reference_image_url=payload.get("reference_image_url"),
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_response(request, exc),
        ) from exc

    return {"account": account.to_dict()}


@router.post("/signup/consent")
async def signup_consent(request: Request, payload: dict) -> dict:
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
        raise HTTPException(
            status_code=400,
            detail=_error_response(request, exc),
        ) from exc

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
async def get_account(request: Request, parent_id: str) -> dict:
    account = await _lifecycle.get_account(parent_id)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail=localized_error_response(
                request,
                code="NOT_FOUND",
                key="auth.not_found",
            ),
        )
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
async def deletion_request(request: Request, payload: dict) -> dict:
    """Request account deletion (starts 30-day window)."""
    parent_id = payload["parent_id"]
    try:
        account = await _lifecycle.request_deletion(parent_id)
    except AuthError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_response(request, exc),
        ) from exc
    return {
        "account": account.to_dict(),
        "deletion_window_remaining_days": account.deletion_window_remaining_days,
    }


@router.post("/deletion/complete")
async def deletion_complete(request: Request, payload: dict) -> dict:
    """Complete the deletion (after 30 days)."""
    parent_id = payload["parent_id"]
    try:
        account = await _lifecycle.complete_deletion(parent_id)
    except AuthError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_response(request, exc),
        ) from exc
    return {"account": account.to_dict()}


@router.get("/consent/document/{consent_type}")
async def get_consent_document(request: Request, consent_type: str) -> dict:
    """Get the consent document text (parent or child version)."""
    if consent_type not in ("parent", "child"):
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="INVALID_CONSENT_TYPE",
                key="auth.invalid_consent_type",
            ),
        )
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
# BE-COMP-006: captcha / Turnstile verification layer (v1 = arithmetic)
# ---------------------------------------------------------------------------


@router.get("/captcha/challenge")
async def captcha_challenge() -> dict:
    """Mint a fresh captcha challenge.

    Returns:
        captcha_id  — opaque token; must be round-tripped through
                      `/auth/captcha/verify` (or sent alongside
                      `/auth/sms/send` when captcha is required).
        question    — human-readable prompt, e.g. "3 + 4 = ?".
        expires_at  — unix epoch seconds; clients should treat the
                      challenge as stale past this point.
    """
    challenge = await get_captcha_service().create_challenge()
    return {
        "captcha_id": challenge.id,
        "question": challenge.question,
        "expires_at": challenge.expires_at,
    }


@router.post("/captcha/verify")
async def captcha_verify(payload: dict) -> dict:
    """Verify a captcha answer. Returns `{valid: bool}` (no error code).

    The endpoint is intentionally idempotent and never 4xxs — the
    client distinguishes a wrong answer by `valid: false`. This
    keeps it composable for frontend retry / shake-to-refresh flows.
    """
    challenge_id = payload.get("captcha_id", "")
    answer = payload.get("answer")
    if not isinstance(challenge_id, str) or answer is None:
        return {"valid": False}
    valid = await get_captcha_service().verify(challenge_id, answer)
    return {"valid": valid}


# ---------------------------------------------------------------------------
# BE-AUTH-002: SMS-code login (mock provider)
# ---------------------------------------------------------------------------


@router.post("/sms/send")
@limiter.limit("3/minute")
async def sms_send(request: Request, payload: dict) -> dict:
    """Send a 6-digit SMS code to `phone`.

    Dev mode echoes the code back so the frontend can use it directly;
    production deployments must NOT return the code in the response.

    BE-COMP-006: captcha gating. When `Settings.sms_captcha_required`
    is True, a valid (captcha_id, captcha_answer) MUST be present in
    the request body — otherwise the call is rejected with 400
    CAPTCHA_INVALID before the SMS provider is even touched. When
    the flag is False (dev / tests / default), captcha is OPTIONAL:
    if the client sends one we still verify it (so the round-trip
    is exercisable), but absence is fine.
    """
    phone = payload.get("phone")
    if not phone or not validate_phone(phone):
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="INVALID_PHONE_FORMAT",
                key="auth.invalid_phone_format",
            ),
        )

    settings = get_settings()
    captcha_id = payload.get("captcha_id")
    captcha_answer = payload.get("captcha_answer")
    if captcha_id is not None or captcha_answer is not None:
        # Client opted into captcha — verify regardless of flag.
        ok = await get_captcha_service().verify(
            str(captcha_id) if captcha_id is not None else "",
            captcha_answer if captcha_answer is not None else "",
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail=localized_error_response(
                    request,
                    code="CAPTCHA_INVALID",
                    key="auth.captcha_invalid",
                    fallback_message=(
                        "Captcha challenge missing, expired, or wrong"
                    ),
                ),
            )
    elif settings.sms_captcha_required:
        # Production mode: captcha is mandatory.
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="CAPTCHA_INVALID",
                key="auth.captcha_required",
                fallback_message=(
                    "Captcha is required to send an SMS code"
                ),
            ),
        )

    try:
        code = await get_sms_service().send(phone)
    except SMSRateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": exc.code,
                "message": get_translator().translate(
                    _AUTH_KEY_BY_CODE.get(exc.code, "auth.sms_rate_limited"),
                    resolve_lang(request),
                    default=exc.message,
                ),
                "retry_after_seconds": int(exc.retry_after_seconds),
            },
        ) from exc
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
            detail=localized_error_response(
                request,
                code="INVALID_PHONE_FORMAT",
                key="auth.invalid_phone_format",
            ),
        )
    if not code or not isinstance(code, str) or len(code) != 6:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="SMS_CODE_INVALID",
                key="auth.sms_code_invalid",
            ),
        )

    try:
        await get_sms_service().verify(phone, code)
    except (SMSCodeInvalid, SMSCodeExpired, SMSTooManyAttempts) as exc:
        raise HTTPException(
            status_code=401,
            detail=_error_response(request, exc),
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
                detail=_error_response(request, exc),
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
            detail=localized_error_response(
                request,
                code="INVALID_REFRESH_TOKEN",
                key="auth.invalid_refresh_token",
                fallback_message="refresh_token required",
            ),
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
            detail=_error_response(request, exc),
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
    request: Request,
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
            detail=localized_error_response(
                request,
                code="MISSING_TOKEN",
                key="auth.missing_token",
                fallback_message="Bearer token required",
            ),
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
            detail=localized_error_response(
                request,
                code="INVALID_TOKEN",
                key="auth.token_invalid",
            ),
        )
    jti = claims.get("jti")
    if jti:
        await get_blacklist().revoke(jti)
    return {"status": "logged_out", "parent_id": parent.id, "revoked_jti": jti}
