"""Integration tests for the FastAPI get_current_parent dependency
and the demo /api/v1/auth/whoami endpoint (BE-AUTH-001).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from niki.auth.jwt_service import issue_access_token
from niki.auth.token_blacklist import (
    InMemoryTokenBlacklist,
    get_blacklist,
    set_blacklist,
)
from niki.main import create_app


@pytest.fixture
def client() -> TestClient:
    """Build a fresh app per test to keep state isolated."""
    # Reset the process blacklist to a clean instance.
    set_blacklist(InMemoryTokenBlacklist())
    return TestClient(create_app())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestWhoami:
    def test_valid_token_returns_claims(self, client: TestClient) -> None:
        token = issue_access_token("parent_demo", ttl_seconds=60)
        resp = client.get("/api/v1/auth/whoami", headers=_bearer(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["parent_id"] == "parent_demo"
        assert "name" in body
        assert "status" in body

    def test_missing_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/whoami")
        assert resp.status_code == 401
        body = resp.json()
        # §6.3 structured error code
        assert body["detail"]["error"] == "MISSING_TOKEN"
        assert "WWW-Authenticate" in resp.headers

    def test_garbage_token_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/auth/whoami",
            headers=_bearer("not-a-jwt"),
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "MALFORMED_TOKEN"

    def test_expired_token_returns_401(self, client: TestClient) -> None:
        import time

        token = issue_access_token("parent_demo", ttl_seconds=1)
        time.sleep(1.5)
        resp = client.get("/api/v1/auth/whoami", headers=_bearer(token))
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "TOKEN_EXPIRED"

    def test_revoked_token_returns_401(self, client: TestClient) -> None:
        token = issue_access_token("parent_demo", ttl_seconds=60)
        claims = __import__(
            "niki.auth.jwt_service", fromlist=["verify_token"]
        ).verify_token(token, __import__(
            "niki.auth.jwt_service", fromlist=["TokenType"]
        ).TokenType.ACCESS)
        # Need to revoke via async API; run sync via the singleton's async method.
        import asyncio

        asyncio.run(get_blacklist().revoke(claims["jti"]))
        resp = client.get("/api/v1/auth/whoami", headers=_bearer(token))
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "TOKEN_REVOKED"

    def test_wrong_scheme_returns_401(self, client: TestClient) -> None:
        token = issue_access_token("parent_demo", ttl_seconds=60)
        resp = client.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": f"Basic {token}"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "MISSING_TOKEN"
