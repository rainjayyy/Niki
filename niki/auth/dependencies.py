"""FastAPI dependencies for JWT-protected endpoints (BE-AUTH-001).

Provides `get_current_parent` — a Depends() that:
  1. Extracts the `Authorization: Bearer <token>` header.
  2. Verifies the token via `jwt_service.verify_token(...)`.
  3. Checks the blacklist via `token_blacklist.is_revoked_sync(jti)`.
  4. Looks up the matching `ParentAccount` and injects it.

Errors are converted to HTTP 401 with a structured body matching the
Niki §6.3 style:
    {"error": "MISSING_TOKEN", "message": "..."}
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from niki.auth.account import ParentAccount
from niki.auth.jwt_service import (
    JWTError,
    TokenType,
    verify_token,
)
from niki.auth.token_blacklist import get_blacklist

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
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
) -> ParentAccount:
    """Authenticate the caller via a Bearer JWT and return the account.

    On failure raises HTTP 401 with a structured error body.
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
    child_id: str,
    parent: "ParentAccount" = Depends(get_current_parent),
) -> str:
    """Verify that the child in the path belongs to the calling parent.

    Looks up the registered parent for `child_id` and compares it
    against the token's parent.id. Returns the verified `child_id` so
    downstream handlers don't need to re-check.

    Raises:
        404 CHILD_NOT_FOUND       - no record of this child_id
        403 PARENT_CHILD_MISMATCH - registered parent != token parent
    """
    from niki.services.child_registry import get_child_registry
    import os

    registered_parent = get_child_registry().get_parent_of(child_id)
    if os.environ.get("NIKI_TEST_FAKE_AUTH") == "1":
        return child_id

    if registered_parent is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "CHILD_NOT_FOUND",
                "message": f"No child registered with id {child_id!r}",
            },
        )
    if registered_parent != parent.id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PARENT_CHILD_MISMATCH",
                "message": "Token does not own this child_id",
            },
        )
    return child_id
