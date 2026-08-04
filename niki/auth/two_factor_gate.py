"""Sensitive-operation 2FA gate (BE-AUTH-007).

`require_2fa_for_sensitive_ops` is a FastAPI dependency
applied to endpoints that mutate high-stakes state
(account deletion, password change, etc.).

Behaviour:
  * If the parent's 2FA is **not** enabled, pass through.
  * Otherwise, one of the following must hold:
      1. JWT scope == `full` AND iat is within
         `Settings.totp_reauth_window_seconds` of now
         (i.e. the caller has freshly re-authenticated via
         `POST /2fa/verify`).
      2. `X-2FA-Code` header presents a valid current TOTP
         (single-use within the validity window — see
         `TwoFactorService.verify_code` for replay prevention).
      3. `X-2FA-Backup-Code` header presents a valid unused
         backup code.
  * If none of these hold, raise 401 with code
    `TWO_FA_REQUIRED`.

Header-based 2FA proof is consumed ONCE per request — successful
verification bumps `last_used_at` so audit can correlate the
sensitive operation with the auth event.

`get_two_factor_code_headers` exposes the (header-name, value)
constants so the API layer can document them in OpenAPI.
"""

from __future__ import annotations

import time
from typing import Annotated, Optional

import structlog
from fastapi import Depends, Header, HTTPException, Request, status

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.auth.jwt_service import SCOPE_FULL_REAUTH
from niki.config.settings import get_settings
from niki.services.two_factor import (
    TwoFactorBackupCodeInvalid,
    TwoFactorInvalidCode,
    TwoFactorNotEnrolled,
    get_two_factor_service,
)

logger = structlog.get_logger(__name__)

HEADER_TOTP_CODE: str = "X-2FA-Code"
HEADER_BACKUP_CODE: str = "X-2FA-Backup-Code"


async def _attempt_header_2fa(
    parent_id: str,
    totp_header: Optional[str],
    backup_header: Optional[str],
) -> Optional[str]:
    """Try header-supplied TOTP / backup code.

    Returns a short label (`"totp"` / `"backup"`) for the audit
    metadata on success, or `None` if no usable header was
    presented (or the code was wrong).
    """
    service = get_two_factor_service()
    if totp_header:
        try:
            await service.verify_code(parent_id, totp_header)
            return "totp"
        except (TwoFactorInvalidCode, TwoFactorNotEnrolled):
            return None
    if backup_header:
        try:
            await service.verify_backup_code(
                parent_id, backup_header
            )
            return "backup"
        except (TwoFactorBackupCodeInvalid, TwoFactorNotEnrolled):
            return None
    return None


async def require_2fa_for_sensitive_ops(
    request: Request,
    parent: Annotated[ParentAccount, Depends(get_current_parent)],
    x_2fa_code: Annotated[
        Optional[str], Header(alias=HEADER_TOTP_CODE)
    ] = None,
    x_2fa_backup_code: Annotated[
        Optional[str], Header(alias=HEADER_BACKUP_CODE)
    ] = None,
) -> bool:
    """Dependency that admits the caller iff 2FA gate passes.

    Always returns the `ParentAccount` so handlers can keep using
    `parent.id`. The gate's success is recorded in
    `request.state.two_factor_verified_via` for downstream audit.

    The annotated parameter name is `parent` (not `_gate`) on
    purpose — FastAPI reads parameter names when introspecting
    the function signature to wire sub-deps, and using `_gate`
    earlier caused FastAPI to mis-parse the dep tree. The name
    appears in OpenAPI as a response model field, never as a
    request field.
    """
    service = get_two_factor_service()
    enabled = await service.is_enabled(parent.id)
    if not enabled:
        # No 2FA enrolled → nothing to check.
        request.state.two_factor_verified_via = "disabled"
        return parent

    # Path 1: fresh full-reauth-scope JWT.
    scope = getattr(request.state, "jwt_scope", SCOPE_FULL_REAUTH)
    iat = getattr(request.state, "jwt_iat", None)
    if scope == SCOPE_FULL_REAUTH and iat is not None:
        now = int(time.time())
        window = get_settings().totp_reauth_window_seconds
        if now - int(iat) <= window:
            request.state.two_factor_verified_via = "scope"
            return parent

    # Path 2 / 3: header-supplied proof.
    via = await _attempt_header_2fa(
        parent.id, x_2fa_code, x_2fa_backup_code
    )
    if via is not None:
        request.state.two_factor_verified_via = via
        return parent

    # Fallthrough: caller has 2FA enabled but didn't prove it.
    logger.warning(
        "two_factor.gate.denied",
        parent_id=parent.id,
        endpoint=f"{request.method} {request.url.path}",
    )
    # §6.3 envelope: raise an HTTPException whose detail IS the
    # envelope. The `error_handler.http_exception_handler` detects the
    # envelope (has `success: false` + `error.code`) and passes it
    # through verbatim instead of wrapping under `{"detail": ...}`.
    from niki.api._i18n import error_response

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=error_response(
            request,
            code="TWO_FA_REQUIRED",
            key="auth.2fa_required",
            fallback_message=(
                "2FA is enabled on this account. Provide "
                f"{HEADER_TOTP_CODE} or {HEADER_BACKUP_CODE} "
                "header, or call POST /auth/2fa/verify first."
            ),
        ),
    )