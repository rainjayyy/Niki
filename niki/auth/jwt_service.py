"""JWT signing and verification service (BE-AUTH-001).

Issues / verifies HS256 tokens for parent authentication.

Token shape:
    {
      "sub":  "<parent_id>",
      "typ":  "access" | "refresh",
      "iat":  <unix-ts>,
      "exp":  <unix-ts>,
      "jti":  "<uuid-v4>"
    }

Two token types:
  - access  : short-lived (1 hour), sent on every authenticated request
  - refresh : long-lived (30 days), only used by POST /auth/refresh

Verification enforces `typ` to prevent access-token-as-refresh attacks.

A pluggable blacklist (niki.auth.token_blacklist) keeps revoked token
`jti` strings so logout can invalidate tokens before their natural
expiry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from enum import StrEnum
from typing import Any

from niki.config.settings import get_settings


class TokenType(StrEnum):
    """Token type claim `typ`."""

    ACCESS = "access"
    REFRESH = "refresh"


class JWTError(Exception):
    """Base class for JWT signing/verification failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _b64_encode(raw: bytes) -> str:
    """URL-safe base64 encode with padding stripped."""
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(s: str) -> bytes:
    """URL-safe base64 decode with implicit padding restore."""
    padding = "=" * (-len(s) % 4)
    return urlsafe_b64decode(s + padding)


def _sign(header_payload: bytes, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        header_payload,
        hashlib.sha256,
    ).digest()
    return _b64_encode(digest)


def _ensure_secret() -> str:
    secret = get_settings().jwt_secret
    if not secret:
        raise JWTError(
            "JWT_SECRET_MISSING",
            "settings.jwt_secret is empty; set JWT_SECRET env var",
        )
    if secret == "change-me-in-production":
        # Soft warning only — tests / dev should be able to use the default.
        import structlog

        structlog.get_logger(__name__).warning(
            "jwt_service.using_default_secret",
            hint="set JWT_SECRET env var in production",
        )
    return secret


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def issue_access_token(
    parent_id: str,
    *,
    ttl_seconds: int | None = None,
) -> str:
    """Sign a short-lived access token.

    Args:
        parent_id: The subject (`sub`) - parent account id.
        ttl_seconds: Override the default 1-hour TTL (tests only).

    Returns:
        Encoded JWT string of the form `header.payload.signature`.
    """
    return _issue(parent_id, TokenType.ACCESS, ttl_seconds)


def issue_refresh_token(
    parent_id: str,
    *,
    ttl_seconds: int | None = None,
) -> str:
    """Sign a long-lived refresh token (30 days by default)."""
    return _issue(parent_id, TokenType.REFRESH, ttl_seconds)


def verify_token(
    token: str,
    expected_type: TokenType,
    *,
    is_revoked: "callable[[str], bool] | None" = None,
) -> dict[str, Any]:
    """Verify a token's signature, expiry, type and revocation state.

    Args:
        token: The encoded JWT string from the Authorization header.
        expected_type: Must equal the token's `typ` claim.
        is_revoked: Optional callable that takes a `jti` and returns True
            if the token was logged out.

    Returns:
        The decoded JWT claims (dict).

    Raises:
        JWTError: With one of:
          - MALFORMED_TOKEN
          - INVALID_SIGNATURE
          - TOKEN_EXPIRED
          - WRONG_TOKEN_TYPE
          - TOKEN_REVOKED
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTError("MALFORMED_TOKEN", "JWT must have 3 segments")

    header_b64, payload_b64, sig_b64 = parts
    secret = _ensure_secret()

    # 1. Signature check
    expected_sig = _sign(f"{header_b64}.{payload_b64}".encode("ascii"), secret)
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise JWTError("INVALID_SIGNATURE", "Token signature does not match")

    # 2. Decode + claim checks
    try:
        claims = json.loads(_b64_decode(payload_b64))
    except Exception as exc:
        raise JWTError("MALFORMED_TOKEN", f"Could not decode payload: {exc}") from exc

    typ = claims.get("typ")
    if typ != expected_type.value:
        raise JWTError(
            "WRONG_TOKEN_TYPE",
            f"Expected token type '{expected_type.value}', got '{typ}'",
        )

    exp = claims.get("exp")
    now = int(time.time())
    if exp is None or now >= int(exp):
        raise JWTError("TOKEN_EXPIRED", "Token has expired")

    # 3. Revocation check
    jti = claims.get("jti")
    if is_revoked is not None and jti and is_revoked(jti):
        raise JWTError("TOKEN_REVOKED", "Token has been revoked")

    return claims


def decode_unverified(token: str) -> dict[str, Any]:
    """Decode JWT claims without verifying signature/expiry/type.

    Useful for diagnostics / logging only. NEVER use for auth decisions.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTError("MALFORMED_TOKEN", "JWT must have 3 segments")
    try:
        return json.loads(_b64_decode(parts[1]))
    except Exception as exc:
        raise JWTError("MALFORMED_TOKEN", f"Could not decode payload: {exc}") from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _issue(
    parent_id: str,
    typ: TokenType,
    ttl_seconds: int | None,
) -> str:
    settings = get_settings()
    if typ == TokenType.ACCESS:
        ttl = ttl_seconds or settings.jwt_access_ttl_seconds
    else:
        ttl = ttl_seconds or settings.jwt_refresh_ttl_seconds

    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": parent_id,
        "typ": typ.value,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    header_b64 = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(f"{header_b64}.{payload_b64}".encode("ascii"), _ensure_secret())
    return f"{header_b64}.{payload_b64}.{signature}"
