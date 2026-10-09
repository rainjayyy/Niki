"""Two-Factor Authentication (2FA) endpoints (BE-AUTH-007).

Routes
------
  POST /api/v1/auth/2fa/enroll           — begin enrollment (gen secret + QR)
  POST /api/v1/auth/2fa/enroll/verify    — confirm first TOTP; enable + return backup codes
  POST /api/v1/auth/2fa/verify           — verify TOTP/backup; upgrade JWT scope to `full`
  DELETE /api/v1/auth/2fa                — disable 2FA (requires re-auth)
  POST /api/v1/auth/2fa/backup-codes     — regenerate backup codes (requires re-auth)
  GET /api/v1/auth/2fa/status            — current enrollment state
  POST /api/v1/auth/2fa/login            — exchange partial_token + TOTP for full tokens

Every state-mutating endpoint writes a row to `audit_logs`
(BE-COMP-004) via `AuditLogService.log_event` so the parent
can see enrollment / verification / disable events alongside
the rest of their account history.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from niki.api._i18n import error_response, localized_error_response
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.auth.jwt_service import (
    SCOPE_2FA_PENDING,
    SCOPE_FULL,
    SCOPE_FULL_REAUTH,
    JWTError,
    TokenType,
    issue_access_token,
    issue_refresh_token,
    verify_token,
)
from niki.auth.token_blacklist import get_blacklist
from niki.config.settings import get_settings
from niki.middleware.rate_limit import limiter
from niki.metrics import get_registry
from niki.services.audit_log import get_audit_log_service
from niki.services.two_factor import (
    TwoFactorAlreadyEnrolled,
    TwoFactorBackupCodeInvalid,
    TwoFactorInvalidCode,
    TwoFactorNotEnrolled,
    TwoFactorReauthRequired,
    get_two_factor_service,
)

router = APIRouter(
    prefix="/auth/2fa",
    tags=[
        {
            "name": "auth-2fa",
            "description": (
                "Parent two-factor authentication (BE-AUTH-007) — "
                "TOTP enrollment, verification, backup-code recovery, "
                "and disable/regenerate flows."
            ),
        },
    ],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


def _reauth_token_from_request(
    payload: dict,
    x_reauth: Optional[str],
) -> str:
    """Read re-auth password from header or body.

    Spec: "header `X-Reauth-Password` or body field".
    The body field name we accept is `password` (the SMS-login
    path uses `code`, signup uses `password`).
    """
    if x_reauth and x_reauth.strip():
        return x_reauth
    pw = payload.get("password")
    if isinstance(pw, str) and pw.strip():
        return pw
    return ""


# ---------------------------------------------------------------------------
# Audit event constants (BE-AUTH-007, BE-COMP-004)
# ---------------------------------------------------------------------------


EVENT_ENROLL_STARTED = "2fa_enrollment_started"
EVENT_ENROLL_COMPLETED = "2fa_enrollment_completed"
EVENT_VERIFY_SUCCESS = "2fa_verification_success"
EVENT_VERIFY_FAILURE = "2fa_verification_failure"
EVENT_DISABLED = "2fa_disabled"
EVENT_BACKUP_USED = "2fa_backup_code_used"
EVENT_LOGIN_SUCCESS = "2fa_login_success"
EVENT_LOGIN_FAILURE = "2fa_login_failure"
EVENT_BACKUP_REGEN = "2fa_backup_codes_regenerated"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/enroll",
    summary="Begin 2FA enrollment",
    description=(
        "BE-AUTH-007 step 1: generate a fresh TOTP secret and return "
        "the otpauth URL + inline SVG QR code. Does NOT enable 2FA "
        "yet — the parent must verify the first code via "
        "`POST /2fa/enroll/verify` before 2FA is considered active."
    ),
)
@limiter.limit("3/hour")
async def enroll(
    request: Request,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
    payload: dict | None = None,
) -> dict:
    """Begin TOTP enrollment for the authenticated parent."""
    try:
        payload = await get_two_factor_service().begin_enrollment(parent.id)
    except TwoFactorAlreadyEnrolled as exc:
        raise HTTPException(
            status_code=409,
            detail=localized_error_response(
                request,
                code=exc.code,
                key="auth.2fa_already_enrolled",
                fallback_message=exc.message,
            ),
        ) from exc

    await get_audit_log_service().log_event(
        EVENT_ENROLL_STARTED,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        endpoint="/api/v1/auth/2fa/enroll",
        method="POST",
        reason="2FA enrollment started",
        client_ip=_client_ip(request),
    )
    return {
        "secret": payload.secret_plaintext,
        "otpauth_url": payload.otpauth_url,
        "qr_code": payload.qr_code,
    }


@router.post(
    "/enroll/verify",
    summary="Verify first TOTP code and enable 2FA",
    description=(
        "BE-AUTH-007 step 2: confirm the parent successfully added "
        "the secret to their authenticator app by submitting the "
        "current 6-digit code. On success, 2FA is enabled and ten "
        "one-time backup codes are returned — they are displayed "
        "ONCE and cannot be retrieved later."
    ),
)
async def enroll_verify(
    request: Request,
    payload: dict,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
) -> dict:
    """Verify first code + enable 2FA + return backup codes."""
    totp_code = payload.get("totp_code")
    if not totp_code or not isinstance(totp_code, str):
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="TWO_FA_INVALID_CODE",
                key="auth.2fa_invalid_code",
            ),
        )
    try:
        backup_codes = await get_two_factor_service().verify_enrollment(parent.id, totp_code)
    except TwoFactorNotEnrolled as exc:
        return JSONResponse(
            status_code=400,
            content=error_response(
                request,
                code=exc.code,
                key="auth.2fa_not_enrolled",
                fallback_message=exc.message,
            ),
        )
    except TwoFactorAlreadyEnrolled as exc:
        return JSONResponse(
            status_code=409,
            content=error_response(
                request,
                code=exc.code,
                key="auth.2fa_already_enrolled",
                fallback_message=exc.message,
            ),
        )
    except TwoFactorInvalidCode as exc:
        get_registry().LOGIN_FAILURE_TOTAL.labels(method="totp", reason=exc.code).inc()
        return JSONResponse(
            status_code=401,
            content=error_response(
                request,
                code=exc.code,
                key="auth.2fa_invalid_code",
                fallback_message=exc.message,
            ),
        )

    await get_audit_log_service().log_event(
        EVENT_ENROLL_COMPLETED,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        endpoint="/api/v1/auth/2fa/enroll/verify",
        method="POST",
        reason="2FA enrollment completed",
        client_ip=_client_ip(request),
    )
    return {
        "status": "enabled",
        "backup_codes": backup_codes,
    }


@router.post(
    "/verify",
    summary="Verify TOTP or backup code (re-auth)",
    description=(
        "Re-authenticate the caller with a current TOTP code or a "
        "backup code. Returns a short-lived (5-minute) full-scope "
        "JWT that `require_2fa_for_sensitive_ops` accepts as proof "
        "for deletion / password change / etc."
    ),
)
@limiter.limit("5/minute")
async def verify(
    request: Request,
    payload: dict,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
) -> dict:
    """Verify TOTP / backup code; issue a 5-min full-scope token."""
    totp_code = payload.get("totp_code")
    backup_code = payload.get("backup_code")
    if not totp_code and not backup_code:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="TWO_FA_INVALID_CODE",
                key="auth.2fa_invalid_code",
            ),
        )
    service = get_two_factor_service()
    method = None
    try:
        if totp_code:
            await service.verify_code(parent.id, totp_code)
            method = "totp"
        else:
            await service.verify_backup_code(parent.id, backup_code)
            method = "backup"
    except TwoFactorNotEnrolled as exc:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code=exc.code,
                key="auth.2fa_not_enrolled",
                fallback_message=exc.message,
            ),
        ) from exc
    except (TwoFactorInvalidCode, TwoFactorBackupCodeInvalid) as exc:
        get_registry().LOGIN_FAILURE_TOTAL.labels(method="totp", reason=exc.code).inc()
        await get_audit_log_service().log_event(
            EVENT_VERIFY_FAILURE,
            severity="warning",
            actor_parent_id=parent.id,
            target_parent_id=parent.id,
            endpoint="/api/v1/auth/2fa/verify",
            method="POST",
            reason=f"2FA verification failed ({method})",
            client_ip=_client_ip(request),
        )
        key = "auth.2fa_backup_code_invalid" if method == "backup" else "auth.2fa_invalid_code"
        raise HTTPException(
            status_code=401,
            detail=localized_error_response(
                request,
                code=exc.code,
                key=key,
                fallback_message=exc.message,
            ),
        ) from exc

    settings = get_settings()
    scope_token = issue_access_token(
        parent.id,
        ttl_seconds=settings.totp_reauth_window_seconds,
        scope=SCOPE_FULL_REAUTH,
    )
    audit_event = EVENT_BACKUP_USED if method == "backup" else EVENT_VERIFY_SUCCESS
    await get_audit_log_service().log_event(
        audit_event,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        endpoint="/api/v1/auth/2fa/verify",
        method="POST",
        reason=f"2FA verified via {method}",
        client_ip=_client_ip(request),
    )
    return {
        "status": "ok",
        "verified_via": method,
        "scope_token": scope_token,
        "expires_in": settings.totp_reauth_window_seconds,
    }


@router.delete(
    "",
    summary="Disable 2FA",
    description=(
        "BE-AUTH-007: turn off 2FA for the authenticated parent. "
        "Requires a non-empty re-auth token (header "
        "`X-Reauth-Password` or body `password` field) — this is "
        "the seam where password re-auth lands once BE-AUTH-008 "
        "introduces a password model."
    ),
)
async def disable(
    request: Request,
    payload: dict,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
    x_reauth_password: Annotated[Optional[str], Header(alias="X-Reauth-Password")] = None,
) -> dict:
    """Disable 2FA for the parent."""
    reauth = _reauth_token_from_request(payload, x_reauth_password)
    try:
        await get_two_factor_service().disable(parent.id, reauth)
    except TwoFactorReauthRequired as exc:
        raise HTTPException(
            status_code=401,
            detail=localized_error_response(
                request,
                code=exc.code,
                key="auth.2fa_password_required",
                fallback_message=exc.message,
            ),
        ) from exc
    except TwoFactorNotEnrolled as exc:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code=exc.code,
                key="auth.2fa_not_enrolled",
                fallback_message=exc.message,
            ),
        ) from exc

    await get_audit_log_service().log_event(
        EVENT_DISABLED,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        endpoint="/api/v1/auth/2fa",
        method="DELETE",
        reason="2FA disabled",
        client_ip=_client_ip(request),
    )
    return {"status": "disabled", "parent_id": parent.id}


@router.post(
    "/backup-codes",
    summary="Regenerate backup codes",
    description=(
        "Issue ten fresh backup codes. The previously-issued set "
        "becomes immediately useless (the bcrypt hash is replaced)."
    ),
)
async def regenerate_backup_codes(
    request: Request,
    payload: dict,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
    x_reauth_password: Annotated[Optional[str], Header(alias="X-Reauth-Password")] = None,
) -> dict:
    """Regenerate the parent's 10 backup codes."""
    reauth = _reauth_token_from_request(payload, x_reauth_password)
    try:
        codes = await get_two_factor_service().regenerate_backup_codes(parent.id, reauth)
    except TwoFactorReauthRequired as exc:
        raise HTTPException(
            status_code=401,
            detail=localized_error_response(
                request,
                code=exc.code,
                key="auth.2fa_password_required",
                fallback_message=exc.message,
            ),
        ) from exc
    except TwoFactorNotEnrolled as exc:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code=exc.code,
                key="auth.2fa_not_enrolled",
                fallback_message=exc.message,
            ),
        ) from exc

    await get_audit_log_service().log_event(
        EVENT_BACKUP_REGEN,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        endpoint="/api/v1/auth/2fa/backup-codes",
        method="POST",
        reason="2FA backup codes regenerated",
        client_ip=_client_ip(request),
    )
    return {"backup_codes": codes}


@router.get(
    "/status",
    summary="2FA enrollment status",
    description=(
        "Return whether 2FA is enabled, when it was enrolled, when "
        "it was last used, and how many backup codes remain."
    ),
)
async def status(
    request: Request,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
) -> dict:
    """Return the parent's 2FA enrollment state."""
    return await get_two_factor_service().status(parent.id)


@router.post(
    "/login",
    summary="Exchange partial_token + TOTP for full tokens",
    description=(
        "Step 2 of the SMS-login flow when the account has 2FA "
        "enabled. `POST /auth/sms/login` returns "
        "`{status: '2fa_required', partial_token}` instead of full "
        "tokens; the client posts that partial_token + a current "
        "TOTP code here to receive the full access + refresh pair."
    ),
)
@limiter.limit("10/minute")
async def login(request: Request, payload: dict) -> dict:
    """Second leg of the SMS-login flow when 2FA is enabled."""
    partial_token = payload.get("partial_token")
    totp_code = payload.get("totp_code")
    if not partial_token or not isinstance(partial_token, str):
        return JSONResponse(
            status_code=400,
            content=error_response(
                request,
                code="INVALID_TOKEN",
                key="auth.token_invalid",
                fallback_message="partial_token required",
            ),
        )
    if not totp_code or not isinstance(totp_code, str):
        return JSONResponse(
            status_code=400,
            content=error_response(
                request,
                code="TWO_FA_INVALID_CODE",
                key="auth.2fa_invalid_code",
            ),
        )
    try:
        claims = verify_token(
            partial_token,
            TokenType.ACCESS,
            is_revoked=get_blacklist().is_revoked_sync,
        )
    except JWTError as exc:
        return JSONResponse(
            status_code=401,
            content=error_response(
                request,
                code=exc.code,
                key="auth.token_invalid",
                fallback_message=exc.message,
            ),
        )
    if claims.get("scope") != SCOPE_2FA_PENDING:
        return JSONResponse(
            status_code=401,
            content=error_response(
                request,
                code="INVALID_PARTIAL_TOKEN",
                key="auth.token_invalid",
                fallback_message=(
                    "The provided token is not a 2fa_pending partial "
                    "token; use /auth/sms/login to obtain one."
                ),
            ),
        )
    parent_id = claims["sub"]

    try:
        await get_two_factor_service().verify_code(parent_id, totp_code)
    except TwoFactorNotEnrolled as exc:
        return JSONResponse(
            status_code=400,
            content=error_response(
                request,
                code=exc.code,
                key="auth.2fa_not_enrolled",
                fallback_message=exc.message,
            ),
        )
    except TwoFactorInvalidCode as exc:
        get_registry().LOGIN_FAILURE_TOTAL.labels(method="totp_2fa_login", reason=exc.code).inc()
        await get_audit_log_service().log_event(
            EVENT_LOGIN_FAILURE,
            severity="warning",
            actor_parent_id=parent_id,
            target_parent_id=parent_id,
            endpoint="/api/v1/auth/2fa/login",
            method="POST",
            reason="2FA login verification failed",
            client_ip=_client_ip(request),
        )
        return JSONResponse(
            status_code=401,
            content=error_response(
                request,
                code=exc.code,
                key="auth.2fa_invalid_code",
                fallback_message=exc.message,
            ),
        )

    settings = get_settings()
    access = issue_access_token(parent_id, scope=SCOPE_FULL)
    refresh = issue_refresh_token(parent_id)
    get_registry().LOGIN_SUCCESS_TOTAL.labels(method="totp_2fa_login").inc()
    await get_audit_log_service().log_event(
        EVENT_LOGIN_SUCCESS,
        actor_parent_id=parent_id,
        target_parent_id=parent_id,
        endpoint="/api/v1/auth/2fa/login",
        method="POST",
        reason="2FA login completed",
        client_ip=_client_ip(request),
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": settings.jwt_access_ttl_seconds,
        "parent_id": parent_id,
        "scope": SCOPE_FULL,
    }
