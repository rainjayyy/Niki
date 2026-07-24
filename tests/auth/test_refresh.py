"""Integration tests for POST /api/v1/auth/refresh (BE-AUTH-003).

Covers:
    - happy path: refresh -> new access + new refresh
    - rotation: old refresh is revoked immediately
    - access-as-refresh rejected
    - expired refresh rejected
    - garbage rejected
    - missing field rejected
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.auth.jwt_service import (
    issue_access_token,
    issue_refresh_token,
    verify_token,
    TokenType,
)
from niki.auth.sms_service import InMemorySMSCodeService, set_sms_service
from niki.auth.lifecycle import AccountLifecycle
from niki.auth.token_blacklist import (
    InMemoryTokenBlacklist,
    set_blacklist,
)
import niki.api.auth as api_auth
from niki.main import create_app


@pytest.fixture
def client() -> TestClient:
    api_auth._lifecycle = AccountLifecycle()
    set_sms_service(InMemorySMSCodeService())
    set_blacklist(InMemoryTokenBlacklist())
    return TestClient(create_app())


class TestRefresh:
    def test_valid_refresh_returns_new_pair(self, client: TestClient) -> None:
        refresh = issue_refresh_token("parent_demo")
        resp = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "Bearer"
        assert body["parent_id"] == "parent_demo"
        assert "access_token" in body and "refresh_token" in body

    def test_new_access_is_access_type(self, client: TestClient) -> None:
        refresh = issue_refresh_token("parent_demo")
        resp = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh}
        )
        new_access = resp.json()["access_token"]
        claims = verify_token(new_access, TokenType.ACCESS)
        assert claims["sub"] == "parent_demo"
        assert claims["typ"] == "access"

    def test_new_refresh_is_refresh_type(self, client: TestClient) -> None:
        refresh = issue_refresh_token("parent_demo")
        resp = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh}
        )
        new_refresh = resp.json()["refresh_token"]
        claims = verify_token(new_refresh, TokenType.REFRESH)
        assert claims["typ"] == "refresh"
        assert claims["sub"] == "parent_demo"

    def test_old_refresh_revoked_after_rotation(self, client: TestClient) -> None:
        refresh = issue_refresh_token("parent_demo")
        # Trigger rotation
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
        # Re-using the OLD refresh must now be rejected.
        resp = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "TOKEN_REVOKED"
        # Reset the shared singleton for downstream test isolation.
        from niki.auth.token_blacklist import set_blacklist, InMemoryTokenBlacklist

        set_blacklist(InMemoryTokenBlacklist())


class TestRefreshErrors:
    def test_access_as_refresh_rejected(self, client: TestClient) -> None:
        access = issue_access_token("parent_demo")
        resp = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": access}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "WRONG_TOKEN_TYPE"

    def test_garbage_refresh_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": "not-a-jwt"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "MALFORMED_TOKEN"

    def test_missing_refresh_rejected(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/refresh", json={})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "INVALID_REFRESH_TOKEN"

    def test_non_string_refresh_rejected(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/refresh", json={"refresh_token": 12345})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "INVALID_REFRESH_TOKEN"

    def test_expired_refresh_rejected(self, client: TestClient) -> None:
        import time

        refresh = issue_refresh_token("parent_demo", ttl_seconds=1)
        time.sleep(1.5)
        resp = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "TOKEN_EXPIRED"
