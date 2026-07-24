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
