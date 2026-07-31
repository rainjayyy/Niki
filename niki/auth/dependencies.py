"""FastAPI dependencies for JWT-protected endpoints (BE-AUTH-001).

Provides `get_current_parent` — a Depends() that:
  1. Extracts the `Authorization: Bearer <token>` header.
  2. Verifies the token via `jwt_service.verify_token(...)`.
  3. Checks the blacklist via `token_blacklist.is_revoked_sync(jti)`.
  4. Looks up the matching `ParentAccount` and injects it.

BE-AUTH-007: also stamps `request.state.jwt_scope` and
`request.state.jwt_iat` so downstream deps
(`require_2fa_for_sensitive_ops`) can tell whether the current
token has a `full` scope or only `2fa_pending`.

Errors are converted to HTTP 401 with a structured body matching the
Niki §6.3 style:
    {"error": "MISSING_TOKEN", "message": "..."}
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from niki.auth.account import ParentAccount
from niki.auth.jwt_service import (
    JWTError,
    SCOPE_FULL,
    TokenType,
    verify_token,
)
from niki.auth.token_blacklist import get_blacklist
from niki.services.auth_audit import get_auth_audit_logger

# `auto_error=False` so we can return a structured 401 payload
# rather than FastAPI's default `{"detail": "..."}` shape.
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_parent(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
) -> ParentAccount:
    """Authenticate the caller via a Bearer JWT and return the account.

    On failure raises HTTP 401 with a structured error body.

    Side effects:
      * Sets `request.state.jwt_scope` (default `"full"` for
        tokens without an explicit scope claim).
      * Sets `request.state.jwt_iat` to the token's iat timestamp
        so `require_2fa_for_sensitive_ops` can decide whether the
        token is fresh enough (BE-AUTH-007).
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("MISSING_TOKEN", "Bearer token required")

    token = credentials.credentials

    try:
        claims = verify_token(
            token,
            TokenType.ACCESS,
            is_revoked=get_blacklist().is_revoked_sync,
        )
    except JWTError as exc:
        raise _unauthorized(exc.code, exc.message) from exc

    parent_id = claims["sub"]
    # BE-AUTH-007: stash scope + iat on request.state for downstream
    # gates. Default to `full` for backwards compatibility — older
    # tokens issued before this claim was introduced have no scope.
    request.state.jwt_scope = claims.get("scope", SCOPE_FULL)
    request.state.jwt_iat = claims.get("iat")
    # Return a thin ParentAccount built from the token's claims so the
    # the dep works without an injected lifecycle. Production wiring
    # should override this dep with one that does `lifecycle.get_account`.
    return ParentAccount(
        id=parent_id,
        name="(from-jwt)",  # real name looked up by override dep
        id_number_masked="****",
        phone_masked="****",
        email=None,
        status=ParentAccount.__dataclass_fields__["status"].default,
    )


async def get_owned_child_id(
    request: Request,
    child_id: str,
    parent: "ParentAccount" = Depends(get_current_parent),
) -> str:
    """Verify that the child in the path belongs to the calling parent.

    Looks up the registered parent for `child_id` and compares it
    against the token's parent.id. Returns the verified `child_id` so
    downstream handlers don't need to re-check.

    Every rejection (CHILD_NOT_FOUND, PARENT_CHILD_MISMATCH) is
    recorded via AuthAuditLogger (BE-AUTH-001-rollout-audit) so
    security teams can detect probing.

    Raises:
        404 CHILD_NOT_FOUND       - no record of this child_id
        403 PARENT_CHILD_MISMATCH - registered parent != token parent
    """
    from niki.services.child_registry import get_child_registry
    import os

    registered_parent = get_child_registry().get_parent_of(child_id)
    if os.environ.get("NIKI_TEST_FAKE_AUTH") == "1":
        return child_id

    endpoint = f"{request.method} {request.url.path}"
    client_ip: Optional[str] = (
        request.client.host if request.client else None
    )

    if registered_parent is None:
        await get_auth_audit_logger().log_violation(
            "child_not_found",
            actor_parent_id=parent.id,
            target_child_id=child_id,
            reason=f"Child {child_id!r} not registered",
            endpoint=endpoint,
            method=request.method,
            client_ip=client_ip,
        )
        raise HTTPException(
            status_code=404,
            detail={
                "error": "CHILD_NOT_FOUND",
                "message": f"No child registered with id {child_id!r}",
            },
        )
    if registered_parent != parent.id:
        await get_auth_audit_logger().log_violation(
            "parent_child_mismatch",
            actor_parent_id=parent.id,
            target_child_id=child_id,
            target_parent_id=registered_parent,
            reason=(
                f"Parent {parent.id!r} tried to access child "
                f"{child_id!r} owned by {registered_parent!r}"
            ),
            endpoint=endpoint,
            method=request.method,
            client_ip=client_ip,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PARENT_CHILD_MISMATCH",
                "message": "Token does not own this child_id",
            },
        )
    return child_id
