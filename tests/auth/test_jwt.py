"""Unit tests for niki.auth.jwt_service (BE-AUTH-001).

Covers:
    - issue/verify roundtrip (both access and refresh)
    - type mismatch rejection
    - expiry rejection
    - bad signature rejection
    - malformed token rejection
"""

from __future__ import annotations

import time

import pytest

from niki.auth.jwt_service import (
    JWTError,
    TokenType,
    issue_access_token,
    issue_refresh_token,
    verify_token,
)


class TestIssueAndVerify:
    def test_access_token_roundtrip(self) -> None:
        token = issue_access_token("parent_abc", ttl_seconds=60)
        claims = verify_token(token, TokenType.ACCESS)
        assert claims["sub"] == "parent_abc"
        assert claims["typ"] == "access"
        assert "exp" in claims
        assert "iat" in claims
        assert "jti" in claims

    def test_refresh_token_roundtrip(self) -> None:
        token = issue_refresh_token("parent_abc", ttl_seconds=3600)
        claims = verify_token(token, TokenType.REFRESH)
        assert claims["typ"] == "refresh"

    def test_unique_jti_per_token(self) -> None:
        t1 = issue_access_token("parent_abc", ttl_seconds=60)
        t2 = issue_access_token("parent_abc", ttl_seconds=60)
        c1 = verify_token(t1, TokenType.ACCESS)
        c2 = verify_token(t2, TokenType.ACCESS)
        assert c1["jti"] != c2["jti"]


class TestTypeEnforcement:
    def test_access_as_refresh_rejected(self) -> None:
        token = issue_access_token("parent_abc", ttl_seconds=60)
        with pytest.raises(JWTError) as exc_info:
            verify_token(token, TokenType.REFRESH)
        assert exc_info.value.code == "WRONG_TOKEN_TYPE"

    def test_refresh_as_access_rejected(self) -> None:
        token = issue_refresh_token("parent_abc", ttl_seconds=60)
        with pytest.raises(JWTError) as exc_info:
            verify_token(token, TokenType.ACCESS)
        assert exc_info.value.code == "WRONG_TOKEN_TYPE"


class TestExpiry:
    def test_expired_token_rejected(self) -> None:
        # Issue with a 1-second TTL then sleep past it.
        token = issue_access_token("parent_abc", ttl_seconds=1)
        time.sleep(1.5)
        with pytest.raises(JWTError) as exc_info:
            verify_token(token, TokenType.ACCESS)
        assert exc_info.value.code == "TOKEN_EXPIRED"


class TestSignature:
    def test_tampered_payload_rejected(self) -> None:
        token = issue_access_token("parent_abc", ttl_seconds=60)
        header, payload, sig = token.split(".")
        # Flip one byte of the payload (still valid base64 of "X").
        tampered = ".".join([header, payload[:-2] + "AA", sig])
        with pytest.raises(JWTError) as exc_info:
            verify_token(tampered, TokenType.ACCESS)
        assert exc_info.value.code == "INVALID_SIGNATURE"

    def test_tampered_signature_rejected(self) -> None:
        token = issue_access_token("parent_abc", ttl_seconds=60)
        header, payload, sig = token.split(".")
        bad_sig = "A" * len(sig)
        tampered = ".".join([header, payload, bad_sig])
        with pytest.raises(JWTError) as exc_info:
            verify_token(tampered, TokenType.ACCESS)
        assert exc_info.value.code == "INVALID_SIGNATURE"


class TestMalformed:
    def test_two_segments_rejected(self) -> None:
        with pytest.raises(JWTError) as exc_info:
            verify_token("abc.def", TokenType.ACCESS)
        assert exc_info.value.code == "MALFORMED_TOKEN"

    def test_four_segments_rejected(self) -> None:
        with pytest.raises(JWTError) as exc_info:
            verify_token("a.b.c.d", TokenType.ACCESS)
        assert exc_info.value.code == "MALFORMED_TOKEN"

    def test_garbage_rejected(self) -> None:
        with pytest.raises(JWTError) as exc_info:
            verify_token("not-a-jwt", TokenType.ACCESS)
        assert exc_info.value.code == "MALFORMED_TOKEN"


class TestRevocationHook:
    def test_revoked_token_rejected(self) -> None:
        token = issue_access_token("parent_abc", ttl_seconds=60)
        claims = verify_token(token, TokenType.ACCESS)

        def _is_revoked(jti: str) -> bool:
            return jti == claims["jti"]

        with pytest.raises(JWTError) as exc_info:
            verify_token(token, TokenType.ACCESS, is_revoked=_is_revoked)
        assert exc_info.value.code == "TOKEN_REVOKED"

    def test_non_revoked_token_passes(self) -> None:
        token = issue_access_token("parent_abc", ttl_seconds=60)

        def _never_revoked(_jti: str) -> bool:
            return False

        claims = verify_token(token, TokenType.ACCESS, is_revoked=_never_revoked)
        assert claims["sub"] == "parent_abc"
