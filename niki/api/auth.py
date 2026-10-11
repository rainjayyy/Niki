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

BE-COMP-004: consent signatures, SMS dispatch and successful logins
are recorded in the unified `audit_logs` table (via
`AuditLogService`) so the parent can see them alongside safety and
deletion events in one history. Those writes never carry PII — no
phone number, no signature text.
"""

from __future__ import annotations
from typing import Any

from typing import Annotated, cast

import structlog

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from niki.auth import (
    AccountLifecycle,
    AuthError,
)
from niki.auth.account import (
    ParentAccount,
)
from niki.auth.dependencies import get_current_parent
from niki.auth.two_factor_gate import (
    require_2fa_for_sensitive_ops,
)
from niki.auth.jwt_service import (
    JWTError,
    SCOPE_2FA_PENDING,
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
from niki.db.repositories.account_repo import persist_parent_account_to_postgres
from niki.db.models.audit_log import (
    EVENT_CONSENT_SIGNED,
    EVENT_LOGIN_SUCCESS,
    EVENT_SMS_SENT,
)
from niki.middleware.rate_limit import limiter
from niki.metrics import get_registry
from niki.services.audit_log import get_audit_log_service
from niki.services.two_factor import get_two_factor_service

logger = structlog.get_logger(__name__)

from niki.api._i18n import error_response, resolve_lang
from niki.i18n.translator import get_translator
from niki.schemas.requests import (
    SMSSendRequest,
    SMSLoginRequest,
)
from niki.services.phone_change import (
    EmailChangeError,
    PhoneChangeError,
    get_email_change_service,
    get_phone_change_service,
)

router = APIRouter(
    prefix="/auth",
    tags=[  
        {  # type: ignore[list-item]
            "name": "auth",
            "description": (
                "Parent authentication (PRD #30, #35, #36) — multi-step "
                "signup, SMS-code login, JWT refresh, and account deletion."
            ),
        },
    ]
)

# Module-level singleton
_lifecycle = AccountLifecycle()


# ---------------------------------------------------------------------------
# BE-AUTH-006: wire the phone/email change services to the unified
# `audit_logs` table (BE-COMP-004). The hook is set lazily so tests
# that override the audit service are picked up.
# ---------------------------------------------------------------------------


def _request_id(request: Request) -> str:
    """Pull the X-Request-ID header or mint a fresh UUIDv4."""
    import uuid as _uuid

    return request.headers.get("X-Request-ID") or str(_uuid.uuid4())


def _audit_hook_factory() -> Any:
    """Build an async hook that defers to the global AuditLogService."""
    from niki.services.audit_log import get_audit_log_service

    async def _hook(
        event_type: str,
        *,
        severity: str = "info",
        actor_parent_id: str | None = None,
        target_parent_id: str | None = None,
        endpoint: str = "",
        method: str = "",
        reason: str = "",
        client_ip: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        try:
            await get_audit_log_service().log_event(
                event_type,
                severity=severity,
                actor_parent_id=actor_parent_id,
                target_parent_id=target_parent_id,
                endpoint=endpoint or None,
                method=method or None,
                reason=reason,
                client_ip=client_ip,
                metadata=metadata or {},
            )
        except Exception:  # pragma: no cover — defence in depth
            pass

    return _hook


# Install the hook on the default singletons so endpoint handlers
# don't have to re-wire it. Tests that inject their own services
# can attach a custom hook via `set_phone_change_service(...)`.
get_phone_change_service()._audit_hook = _audit_hook_factory()
get_email_change_service()._audit_hook = _audit_hook_factory()

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
    # BE-AUTH-006
    "PHONE_CHANGE_INVALID_PASSWORD": "auth.phone_change_invalid_password",
    "PHONE_CHANGE_CODE_INVALID": "auth.phone_change_code_invalid",
    "PHONE_CHANGE_CODE_EXPIRED": "auth.phone_change_code_expired",
    "PHONE_CHANGE_NOT_FOUND": "auth.phone_change_not_found",
    "PHONE_CHANGE_ALREADY_PENDING": "auth.phone_change_already_pending",
    "PHONE_CHANGE_IN_COOLING_PERIOD": ("auth.phone_change_in_cooling_period"),
    "PHONE_CHANGE_SAME_NUMBER": "auth.phone_change_same_number",
    "PHONE_CHANGE_CANNOT_CANCEL": "auth.phone_change_cannot_cancel",
    "EMAIL_CHANGE_INVALID_FORMAT": "auth.email_change_invalid_format",
    "EMAIL_CHANGE_CODE_INVALID": "auth.email_change_code_invalid",
    "EMAIL_CHANGE_NOT_FOUND": "auth.email_change_not_found",
    "EMAIL_ALREADY_IN_USE": "auth.email_already_in_use",
}


def _error_response(request: Request, exc: AuthError) -> dict:
    """Render an AuthError as the full §6.3 response envelope.

    Returns the `{"success": false, "error": {...}, ...}` shape so
    callers can do `return JSONResponse(status_code=..., content=...)`
    instead of `raise HTTPException(detail=...)` — the latter wraps
    the payload in a `{"detail": ...}` envelope that's incompatible
    with the rest of the API.

    The localized `message` field is rendered through the i18n
    catalog using the request's resolved language. The exception's
    own `.message` (English, set by `AuthError.__init__`) is used
    as the fallback when no catalog entry exists yet for the code.
    """
    key = _AUTH_KEY_BY_CODE.get(exc.code)
    if key is not None:
        return error_response(
            request,
            code=exc.code,
            key=key,
            fallback_message=exc.message,
        )
    # Unknown code → keep the exception's literal message so we
    # never regress existing error text.
    return error_response(
        request,
        code=exc.code,
        key="",
        fallback_message=exc.message,
    )


# ---------------------------------------------------------------------------
# BE-AUTH-006: phone/email change error → HTTP status mapping
# ---------------------------------------------------------------------------


# Each entry: error code → (HTTP status, i18n key, fallback message).
# The HTTP status matches the §6.3 contract — 409 for "conflict
# with current state", 422 for "input format error", etc.
_PHONE_CHANGE_STATUS: dict[str, int] = {
    "PHONE_CHANGE_INVALID_PASSWORD": 401,
    "PHONE_CHANGE_NOT_FOUND": 404,
    "PHONE_CHANGE_SAME_NUMBER": 409,
    "PHONE_CHANGE_ALREADY_PENDING": 409,
    "PHONE_CHANGE_CODE_INVALID": 400,
    "PHONE_CHANGE_CODE_EXPIRED": 400,
    "PHONE_CHANGE_CANNOT_CANCEL": 409,
    "PHONE_CHANGE_IN_COOLING_PERIOD": 409,
}
_EMAIL_CHANGE_STATUS: dict[str, int] = {
    "EMAIL_CHANGE_INVALID_FORMAT": 422,
    "EMAIL_CHANGE_CODE_INVALID": 400,
    "EMAIL_CHANGE_NOT_FOUND": 404,
    "EMAIL_ALREADY_IN_USE": 409,
}


def _raise_change_error(request: Request, exc: PhoneChangeError | EmailChangeError) -> JSONResponse:
    """Map a phone/email change service exception to a §6.3 JSONResponse.

    Centralised so endpoints don't repeat the same boilerplate.
    Returns a `JSONResponse` with the full envelope — callers do
    `return _raise_change_error(request, exc)`.
    """
    if isinstance(exc, EmailChangeError):
        status = _EMAIL_CHANGE_STATUS.get(exc.code, 400)
    else:
        status = _PHONE_CHANGE_STATUS.get(exc.code, 400)
    return JSONResponse(
        status_code=status,
        content=_error_response(request, exc),
    )


def _client_ip(request: Request) -> str | None:
    """Best-effort caller IP for the audit trail.

    `X-Forwarded-For` wins when present (we sit behind a proxy in
    every deployed environment); its first hop is the real client.
    Returns None rather than a placeholder so the audit row honestly
    records "we don't know".
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


@router.post(
    "/signup/start",
    summary="Begin parent signup",
    description=(
        "Begin parent signup (step 0). Creates a PENDING account and "
        "returns the parent's progress through the multi-step "
        "verification flow (real-name, face, consent)."
    ),
)
async def signup_start(request: Request, payload: dict) -> dict:
    """Begin signup. Creates a PENDING account."""
    try:
        account, progress = await _lifecycle.start_signup(
            name=payload["name"],
            id_number=payload["id_number"],
            phone=payload["phone"],
            email=payload.get("email"),
        )
        try:
            await persist_parent_account_to_postgres(
                parent_id=account.id,
                name=payload["name"],
                id_number=payload["id_number"],
                phone=payload["phone"],
                email=payload.get("email"),
            )
        except Exception as exc:
            logger.warning("auth.parent_persist_failed", error=str(exc))
    except AuthError as exc:
        return JSONResponse(status_code=400, content=_error_response(request, exc))  # type: ignore[return-value]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Missing field: {exc}")

    return {
        "account": account.to_dict(),
        "progress": {
            "parent_id": progress.parent_id,
            "steps": [{"name": s.name, "completed": s.completed} for s in progress.steps],
        },
    }


@router.post(
    "/signup/real-name",
    summary="Step 1: Real-name verification",
    description=(
        "Step 1 of the signup flow. Verifies the parent's name against "
        "the supplied national ID number and advances progress."
    ),
)
async def signup_real_name(request: Request, payload: dict) -> dict:
    """Step 1: Real-name verification."""
    try:
        account = await _lifecycle.verify_real_name(
            parent_id=payload["parent_id"],
            name=payload["name"],
            id_number=payload["id_number"],
        )
    except AuthError as exc:
        return JSONResponse(status_code=400, content=_error_response(request, exc))  # type: ignore[return-value]

    return {"account": account.to_dict()}


@router.post(
    "/signup/face",
    summary="Step 2: Face verification",
    description=(
        "Step 2 of the signup flow. Compares a live selfie against the "
        "reference image (e.g. extracted from the ID card) for liveness."
    ),
)
async def signup_face(request: Request, payload: dict) -> dict:
    """Step 2: Face verification."""
    try:
        account = await _lifecycle.verify_face(
            parent_id=payload["parent_id"],
            live_image_url=payload["live_image_url"],
            reference_image_url=payload.get("reference_image_url"),
        )
    except AuthError as exc:
        return JSONResponse(status_code=400, content=_error_response(request, exc))  # type: ignore[return-value]

    return {"account": account.to_dict()}


@router.post(
    "/signup/consent",
    summary="Step 3: Sign consent document",
    description=(
        "Step 3 of the signup flow. Records the typed signature (e-sign) "
        "against the parent (and optionally child) consent documents and "
        "promotes the account to ACTIVE."
    ),
)
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
        return JSONResponse(status_code=400, content=_error_response(request, exc))  # type: ignore[return-value]

    # BE-COMP-004: consent is the single most compliance-relevant
    # event on the account — PIPL Art. 14 requires we can show when
    # and by whom it was given. Metadata carries the document
    # versions, never the signature itself.
    await get_audit_log_service().log_event(
        EVENT_CONSENT_SIGNED,
        actor_parent_id=account.id,
        target_parent_id=account.id,
        endpoint="/api/v1/auth/signup/consent",
        method="POST",
        reason="Parent e-signed the consent document(s)",
        metadata={
            "signed_child_consent": bool(payload.get("sign_child_consent", True)),
            "account_status": (
                account.status.value if hasattr(account.status, "value") else str(account.status)
            ),
        },
        client_ip=_client_ip(request),
    )

    return {"account": account.to_dict()}


@router.get(
    "/whoami",
    summary="Inspect current JWT",
    description=(
        "BE-AUTH-001 demo endpoint — proves the JWT dependency works. "
        "Returns the authenticated caller's account id and status from "
        "the JWT subject."
    ),
)
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


@router.get(
    "/me/{parent_id}",
    summary="Get parent account",
    description=(
        "Return a parent account record and its current signup "
        "progress. Returns 404 NOT_FOUND if the parent_id has never "
        "been registered."
    ),
)
async def get_account(request: Request, parent_id: str) -> dict:
    account = await _lifecycle.get_account(parent_id)
    if account is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content=error_response(
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


@router.post(
    "/deletion/request",
    summary="Request account deletion",
    description=(
        "Begin the 30-day deletion window. The account is immediately "
        "flipped to DELETION_PENDING; calling /deletion/complete after "
        "the grace period performs the irreversible purge. "
        "BE-AUTH-007: when the caller's 2FA is enabled, this endpoint "
        "requires either an `X-2FA-Code` or `X-2FA-Backup-Code` "
        "header, or a fresh full-scope JWT issued via "
        "`POST /auth/2fa/verify`."
    ),
)
async def deletion_request(
    request: Request,
    payload: dict,
    _gate_ok: Annotated[  # noqa: ARG001
        bool, Depends(require_2fa_for_sensitive_ops)
    ],
) -> dict:
    """Request account deletion (starts 30-day window).

    BE-AUTH-007: gated by `require_2fa_for_sensitive_ops`.
    """
    parent_id = payload["parent_id"]
    try:
        account = await _lifecycle.request_deletion(parent_id)
    except AuthError as exc:
        return JSONResponse(status_code=400, content=_error_response(request, exc))  # type: ignore[return-value]
    return {
        "account": account.to_dict(),
        "deletion_window_remaining_days": account.deletion_window_remaining_days,
    }


@router.post(
    "/deletion/complete",
    summary="Complete account deletion",
    description=(
        "Complete the deletion (after the 30-day window). Wipes the "
        "parent account, consent records, and audited side-tables."
    ),
)
async def deletion_complete(request: Request, payload: dict) -> dict:
    """Complete the deletion (after 30 days)."""
    parent_id = payload["parent_id"]
    try:
        account = await _lifecycle.complete_deletion(parent_id)
    except AuthError as exc:
        return JSONResponse(status_code=400, content=_error_response(request, exc))  # type: ignore[return-value]
    return {"account": account.to_dict()}


@router.get(
    "/consent/document/{consent_type}",
    summary="Get consent document text",
    description=(
        "Return the human-readable consent document text (parent or "
        "child version) plus its current version string. The frontend "
        "renders this directly to the user before they e-sign."
    ),
)
async def get_consent_document(request: Request, consent_type: str) -> dict:
    """Get the consent document text (parent or child version)."""
    if consent_type not in ("parent", "child"):
        return JSONResponse(  # type: ignore[return-value]
            status_code=400,
            content=error_response(
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


@router.get(
    "/captcha/challenge",
    summary="Mint a captcha challenge",
    description=(
        "BE-COMP-006: mint a fresh captcha challenge for the client. "
        "Returns the challenge id, the human-readable question, and "
        "an expiry timestamp."
    ),
)
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


@router.post(
    "/captcha/verify",
    summary="Verify a captcha answer",
    description=(
        "Verify a captcha answer. Returns `{valid: bool}` (no error "
        "code) — idempotent and never 4xx so frontend retry / "
        "shake-to-refresh flows compose cleanly."
    ),
)
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


@router.post(
    "/sms/send",
    summary="Send a 6-digit SMS code",
    description=(
        "Send a 6-digit SMS code to `phone`. Dev mode echoes the code "
        "back so the frontend can use it directly; production must "
        "NEVER return the code. BE-COMP-006: captcha gating — when "
        "Settings.sms_captcha_required is True a valid (captcha_id, "
        "captcha_answer) pair is mandatory."
    ),
)
# IMPORTANT: keep `payload` typed as the Pydantic model below
# (`SMSSendRequest`) — FastAPI validates it BEFORE the @limiter.limit
# decorator fires, so bad requests return 400/422 without burning
# rate-limit budget. Replacing this with `payload: dict` regresses the
# P2 bug surfaced by frontend testing (bad phone → 429 on the 4th
# call instead of 400 INVALID_PHONE_FORMAT every time).
@limiter.limit(get_settings().sms_send_rate_limit)
async def sms_send(
    request: Request,
    payload: SMSSendRequest,
) -> dict:
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
    phone = payload.phone

    settings = get_settings()
    captcha_id = payload.captcha_id
    captcha_answer = payload.captcha_answer

    if captcha_id is not None or captcha_answer is not None:
        # Client opted into captcha — verify regardless of flag.
        ok = await get_captcha_service().verify(
            str(captcha_id) if captcha_id is not None else "",
            captcha_answer if captcha_answer is not None else "",
        )
        if not ok:
            return JSONResponse(  # type: ignore[return-value]
                status_code=400,
                content=error_response(
                    request,
                    code="CAPTCHA_INVALID",
                    key="auth.captcha_invalid",
                    fallback_message=("Captcha challenge missing, expired, or wrong"),
                ),
            )
    elif settings.sms_captcha_required:
        # Production mode: captcha is mandatory.
        return JSONResponse(  # type: ignore[return-value]
            status_code=400,
            content=error_response(
                request,
                code="CAPTCHA_INVALID",
                key="auth.captcha_required",
                fallback_message=("Captcha is required to send an SMS code"),
            ),
        )

    try:
        code = await get_sms_service().send(phone)
    except SMSRateLimited as exc:
        # SMS service's own per-phone cooldown (60s after consume) —
        # NOT the slowapi 3/minute rate limit. Use the §6.3 structured
        # shape so the frontend interceptor treats it like every other
        # 4xx; the slowapi 429 (RATE_LIMIT_EXCEEDED) uses the same
        # shape via `niki.middleware.rate_limit`.
        msg = get_translator().translate(
            _AUTH_KEY_BY_CODE.get(exc.code, "auth.sms_rate_limited"),
            resolve_lang(request),
            default=exc.message,
        )
        body = {
            "success": False,
            "data": None,
            "error": {
                "code": exc.code,
                "message": msg,
                "detail": {
                    "retry_after_seconds": int(exc.retry_after_seconds),
                },
            },
            "error_code": exc.code,
            "error_detail": {
                "retry_after_seconds": int(exc.retry_after_seconds),
            },
            "request_id": _request_id(request),
        }
        return JSONResponse(status_code=429, content=body)  # type: ignore[return-value]
    body: dict = {"phone": phone, "status": "sent"}
    if settings.debug:
        # Echo the code in DEBUG only; production must NEVER do this.
        body["dev_code"] = code

    # BE-COMP-004: record that a code was sent. No parent id exists
    # yet (this is pre-auth) and the phone number is PII, so the row
    # carries only the fact + the captcha posture. Volume of these
    # rows per IP is what makes SMS-pumping abuse visible.
    await get_audit_log_service().log_event(
        EVENT_SMS_SENT,
        endpoint="/api/v1/auth/sms/send",
        method="POST",
        reason="SMS verification code dispatched",
        metadata={"captcha_presented": captcha_id is not None},
        client_ip=_client_ip(request),
    )
    return body


@router.post(
    "/sms/login",
    summary="SMS-code login",
    description=(
        "Verify the SMS code and issue a JWT access + refresh pair. "
        "On a brand-new phone the lifecycle auto-creates a PENDING "
        "account so login doubles as first-time signup."
    ),
)
@limiter.limit("10/minute")
async def sms_login(
    request: Request,
    payload: SMSLoginRequest,
) -> dict:
    """Verify the SMS code and issue a JWT access + refresh pair.

    On a brand-new phone the lifecycle auto-creates a PENDING account
    so login doubles as first-time signup.

    `payload` is typed as `SMSLoginRequest` so FastAPI validates
    `phone` (11-digit Chinese mobile) and `code` (6-digit numeric)
    BEFORE the @limiter.limit decorator fires. Bad requests are
    rejected at the validation layer (400/422) without burning
    rate-limit budget.
    """
    phone = payload.phone
    code = payload.code

    try:
        await get_sms_service().verify(phone, code)
    except (SMSCodeInvalid, SMSCodeExpired, SMSTooManyAttempts) as exc:
        # BE-API-009: count the failure before raising.
        reason = getattr(exc, "code", None) or type(exc).__name__.lower()
        get_registry().LOGIN_FAILURE_TOTAL.labels(
            method="sms",
            reason=reason,
        ).inc()
        return JSONResponse(status_code=401, content=_error_response(request, cast("AuthError", exc)))  # type: ignore[return-value]

    # Find or auto-create the account.
    account = await _lifecycle.find_by_phone(phone)
    auto_created = account is None
    if account is None:
        # Auto-create PENDING parent. BE-AUTH-002 v1 placeholder: id_number
        # uses a fixed valid value to satisfy format validation; the
        # followup ticket (BE-AUTH-002-followup) adds a proper guided
        # ID-verification flow.
        try:
            account, _ = await _lifecycle.start_signup(
                name=f"家长{phone[-4:]}",
                id_number=f"1101011990{phone[-8:]}",
                phone=phone,
            )
            try:
                await persist_parent_account_to_postgres(
                    parent_id=account.id,
                    name=f"家长{phone[-4:]}",
                    id_number=f"1101011990{phone[-8:]}",
                    phone=phone,
                )
            except Exception as exc:
                logger.warning("auth.parent_persist_failed_sms", error=str(exc))
        except AuthError as exc:
            return JSONResponse(status_code=400, content=_error_response(request, exc))  # type: ignore[return-value]

    settings = get_settings()

    # BE-AUTH-007: when the account has 2FA enabled, the SMS
    # login must NOT issue full tokens. Instead we issue a
    # short-lived `2fa_pending` partial token that the client
    # exchanges at `POST /auth/2fa/login` for the full pair.
    twofa_enabled = await get_two_factor_service().is_enabled(account.id)
    if twofa_enabled:
        partial = issue_access_token(
            account.id,
            ttl_seconds=settings.totp_partial_ttl_seconds,
            scope=SCOPE_2FA_PENDING,
        )
        get_registry().LOGIN_SUCCESS_TOTAL.labels(method="sms").inc()
        await get_audit_log_service().log_event(
            EVENT_LOGIN_SUCCESS,
            actor_parent_id=account.id,
            target_parent_id=account.id,
            endpoint="/api/v1/auth/sms/login",
            method="POST",
            reason="SMS-code login succeeded (2fa pending)",
            metadata={
                "auto_created_account": auto_created,
                "account_status": account.status.value,
                "twofa_required": True,
            },
            client_ip=_client_ip(request),
        )
        return {
            "status": "2fa_required",
            "partial_token": partial,
            "token_type": "Bearer",
            "expires_in": settings.totp_partial_ttl_seconds,
            "parent_id": account.id,
            "account_status": account.status.value,
        }

    access = issue_access_token(account.id)
    refresh = issue_refresh_token(account.id)

    # BE-API-009: count successful SMS logins.
    get_registry().LOGIN_SUCCESS_TOTAL.labels(method="sms").inc()

    # BE-COMP-004: a successful login is the anchor event for every
    # "was this me?" support conversation. `auto_created` tells the
    # parent apart from a first-time signup on a fresh phone.
    await get_audit_log_service().log_event(
        EVENT_LOGIN_SUCCESS,
        actor_parent_id=account.id,
        target_parent_id=account.id,
        endpoint="/api/v1/auth/sms/login",
        method="POST",
        reason="SMS-code login succeeded",
        metadata={
            "auto_created_account": auto_created,
            "account_status": account.status.value,
        },
        client_ip=_client_ip(request),
    )

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


@router.post(
    "/refresh",
    summary="Refresh access + refresh token",
    description=(
        "BE-AUTH-003: trade a refresh token for a fresh access + "
        "refresh pair. The presented refresh token is verified "
        "(signature + type + expiry) and immediately revoked so it "
        "cannot be re-used (rotation)."
    ),
)
@limiter.limit("60/minute")
async def refresh(request: Request, payload: dict) -> dict:
    """Trade a refresh token for a fresh access + refresh pair.

    The presented refresh token is verified (signature + type + expiry)
    and, on success, immediately revoked so it cannot be re-used.
    A NEW refresh token is issued (rotation).
    """
    refresh_token = payload.get("refresh_token")
    if not refresh_token or not isinstance(refresh_token, str):
        return JSONResponse(  # type: ignore[return-value]
            status_code=400,
            content=error_response(
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
        return JSONResponse(status_code=401, content=_error_response(request, cast("AuthError", exc)))  # type: ignore[return-value]

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


@router.post(
    "/logout",
    summary="Logout (revoke access JWT)",
    description=(
        "BE-AUTH-004: revoke the presented access token by jti. "
        "Idempotent — re-logging-out the same token returns 200 "
        "(already-revoked tokens are rejected upstream by the dep)."
    ),
)
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
        return JSONResponse(  # type: ignore[return-value]
            status_code=401,
            content=error_response(
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
        return JSONResponse(  # type: ignore[return-value]
            status_code=401,
            content=error_response(
                request,
                code="INVALID_TOKEN",
                key="auth.token_invalid",
            ),
        )
    jti = claims.get("jti")
    if jti:
        await get_blacklist().revoke(jti)
    return {"status": "logged_out", "parent_id": parent.id, "revoked_jti": jti}


# ---------------------------------------------------------------------------
# BE-AUTH-006: phone/email change endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/phone/change/request",
    summary="Request phone change (step 1)",
    description=(
        "BE-AUTH-006: begin a phone-change flow. The parent must "
        "supply their current `password` (re-auth) and the `new_phone` "
        "to bind. The backend dispatches a 6-digit SMS code to "
        "`new_phone` and persists a `pending_verification` row with a "
        "10-minute expiry. Rate-limited to 3 requests per minute per IP."
    ),
)
@limiter.limit("3/minute")
async def phone_change_request(
    request: Request,
    payload: dict,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
) -> dict:
    """Step 1: send SMS code to the new phone."""
    new_phone = payload.get("new_phone", "")
    password = payload.get("password", "")
    try:
        view = await get_phone_change_service().request(
            parent_id=parent.id,
            new_phone=new_phone,
            password=password,
        )
    except PhoneChangeError as exc:
        return _raise_change_error(request, exc)
    except AuthError as exc:
        return JSONResponse(status_code=400, content=_error_response(request, exc))  # type: ignore[return-value]
    return view_to_dict(view, "phone")


@router.post(
    "/phone/change/verify",
    summary="Verify phone-change SMS code (step 2)",
    description=(
        "BE-AUTH-006: verify the 6-digit SMS code that was sent to the "
        "new phone. On success, the request transitions to "
        "`pending_cooling` and `effective_at` is set 7 days into the "
        "future. After `effective_at` a cron sweep completes the swap."
    ),
)
async def phone_change_verify(
    request: Request,
    payload: dict,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
) -> dict:
    """Step 2: verify the SMS code and enter the 7-day cooling period."""
    request_id = payload.get("request_id", "")
    code = payload.get("sms_code", "")
    try:
        view = await get_phone_change_service().verify(
            request_id=request_id,
            sms_code=code,
        )
    except PhoneChangeError as exc:
        return _raise_change_error(request, exc)
    if view.parent_id != parent.id:
        # Lateral-auth: never reveal the row exists if it doesn't
        # belong to the caller.
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content=error_response(
                request,
                code="PHONE_CHANGE_NOT_FOUND",
                key="auth.phone_change_not_found",
            ),
        )
    return view_to_dict(view, "phone")


@router.post(
    "/phone/change/{request_id}/cancel",
    summary="Cancel a phone-change request",
    description=(
        "BE-AUTH-006: cancel a phone-change request that is still in "
        "`pending_verification` or `pending_cooling`. The call is "
        "rejected (404) if the request belongs to a different parent."
    ),
)
async def phone_change_cancel(
    request: Request,
    request_id: str,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
    _body: dict | None = Body(default=None),
) -> dict:
    """Cancel a phone-change request (lateral-auth enforced)."""
    try:
        view = await get_phone_change_service().cancel(
            request_id=request_id,
            parent_id=parent.id,
        )
    except PhoneChangeError as exc:
        _raise_change_error(request, exc)
    return view_to_dict(view, "phone")


@router.post(
    "/phone/change/confirm-now",
    summary="Skip the 7-day cooling period",
    description=(
        "BE-AUTH-006: bypass the cooling period and complete the "
        "phone swap immediately. A `warning`-severity audit event is "
        "written to record the user actively waived the 7-day window."
    ),
)
async def phone_change_confirm_now(
    request: Request,
    payload: dict,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
) -> dict:
    """Bypass the 7-day cooling period (warning-audited)."""
    request_id = payload.get("request_id", "")
    try:
        view = await get_phone_change_service().confirm_now(
            request_id=request_id,
            parent_id=parent.id,
        )
    except PhoneChangeError as exc:
        _raise_change_error(request, exc)
    return view_to_dict(view, "phone")


@router.post(
    "/email/change/request",
    summary="Request email change (step 1)",
    description=(
        "BE-AUTH-006: begin an email-change flow. The backend dispatches "
        "a 6-digit code to `new_email` (mock — logs the code in dev). "
        "Rate-limited to 3 requests per minute per IP."
    ),
)
@limiter.limit("3/minute")
async def email_change_request(
    request: Request,
    payload: dict,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
) -> dict:
    """Step 1: send verification email to the new address."""
    new_email = payload.get("new_email", "")
    try:
        result = await get_email_change_service().request(
            parent_id=parent.id,
            new_email=new_email,
        )
    except EmailChangeError as exc:
        _raise_change_error(request, exc)
    except AuthError as exc:
        return JSONResponse(status_code=400, content=_error_response(request, exc))  # type: ignore[return-value]
    return result


@router.post(
    "/email/change/verify",
    summary="Verify email-change code and complete (step 2)",
    description=(
        "BE-AUTH-006: verify the 6-digit email code. The new email is "
        "swapped into the account immediately; no cooling period."
    ),
)
async def email_change_verify(
    request: Request,
    payload: dict,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
) -> dict:
    """Step 2: verify the email code and apply the swap immediately."""
    code = payload.get("code", "")
    new_email = payload.get("new_email", "")
    try:
        account = await get_email_change_service().verify(
            parent_id=parent.id,
            code=code,
            new_email=new_email,
        )
    except EmailChangeError as exc:
        _raise_change_error(request, exc)
    return {"account": account.to_dict()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def view_to_dict(view: Any, kind: str) -> dict[str, object]:
    """Serialise a PhoneChangeRequestView for API responses.

    `kind` is "phone" today; reserved so future change services can
    share the same JSON-shaping helper.
    """
    return {
        "request_id": view.request_id,
        "parent_id": view.parent_id,
        "status": view.status,
        "requested_at": view.requested_at.isoformat() if view.requested_at else None,
        "expires_at": view.expires_at.isoformat() if view.expires_at else None,
        "effective_at": view.effective_at.isoformat() if view.effective_at else None,
        "confirmed_at": view.confirmed_at.isoformat() if view.confirmed_at else None,
        "completed_at": view.completed_at.isoformat() if view.completed_at else None,
        "cancelled_at": view.cancelled_at.isoformat() if view.cancelled_at else None,
    }
