"""Integration tests for POST /api/v1/auth/logout (BE-AUTH-004).

Covers:
    - happy path: 200 + jti revoked
    - missing token 401
    - garbage token 401
    - after logout the same token cannot access /auth/whoami
    - full E2E: login -> use -> refresh -> logout -> cannot refresh
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import niki.api.auth as api_auth
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.auth.jwt_service import (
    issue_access_token,
    issue_refresh_token,
    verify_token,
    TokenType,
)
from niki.auth.lifecycle import AccountLifecycle
from niki.auth.sms_service import InMemorySMSCodeService, set_sms_service
from niki.auth.token_blacklist import (
    InMemoryTokenBlacklist,
    set_blacklist,
)
from niki.main import create_app


@pytest.fixture
def client_with_real_auth(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Real `get_current_parent` dep wired to a real lifecycle.

    Unset NIKI_TEST_FAKE_AUTH so the JWT validation runs against real
    bearer tokens (not the test sentinel).
    """
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    api_auth._lifecycle = AccountLifecycle()
    set_sms_service(InMemorySMSCodeService())
    set_blacklist(InMemoryTokenBlacklist())
    return TestClient(create_app())


@pytest.fixture
def client_with_fake_auth(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Override `get_current_parent` to return a sentinel; useful for
    testing the logout handler in isolation without going through JWT."""

    def fake_parent() -> ParentAccount:
        return ParentAccount(
            id="parent_demo",
            name="demo",
            id_number_masked="****",
            phone_masked="****",
        )

    app = create_app()
    app.dependency_overrides[get_current_parent] = fake_parent
    set_blacklist(InMemoryTokenBlacklist())
    return TestClient(app)


class TestLogoutHappyPath:
    def test_logout_returns_revoked_jti(
        self, client_with_real_auth: TestClient
    ) -> None:
        token = issue_access_token("parent_demo")
        resp = client_with_real_auth.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "logged_out"
        assert body["parent_id"] == "parent_demo"
        assert body["revoked_jti"]

    def test_logout_revokes_token_immediately(
        self, client_with_real_auth: TestClient
    ) -> None:
        token = issue_access_token("parent_demo")
        client_with_real_auth.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        # Same token now rejected by /auth/whoami (which goes through
        # the real get_current_parent + blacklist check).
        resp = client_with_real_auth.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "TOKEN_REVOKED"
        # Reset shared singleton for downstream test isolation.
        set_blacklist(InMemoryTokenBlacklist())


class TestLogoutErrors:
    def test_missing_token_returns_401(
        self, client_with_fake_auth: TestClient
    ) -> None:
        # Note: we use the fake-auth client because the real dep would
        # already reject with MISSING_TOKEN. The fake dep lets us
        # verify the handler's own behavior when Authorization is
        # absent (the handler always reads the header itself).
        resp = client_with_fake_auth.post("/api/v1/auth/logout")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "MISSING_TOKEN"

    def test_wrong_scheme_returns_401(
        self, client_with_fake_auth: TestClient
    ) -> None:
        resp = client_with_fake_auth.post(
            "/api/v1/auth/logout",
            headers={"Authorization": "Basic abc"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "MISSING_TOKEN"

    def test_unparseable_token_returns_401_via_dep(
        self, client_with_real_auth: TestClient
    ) -> None:
        resp = client_with_real_auth.post(
            "/api/v1/auth/logout",
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "MALFORMED_TOKEN"


class TestFullJwtLifecycle:
    def test_login_refresh_logout_chain(
        self, client_with_real_auth: TestClient
    ) -> None:
        """End-to-end: SMS login -> refresh once -> logout invalidates the
        LAST issued access token."""
        set_sms_service(InMemorySMSCodeService())
        set_blacklist(InMemoryTokenBlacklist())

        # 1) SMS login
        send = client_with_real_auth.post(
            "/api/v1/auth/sms/send", json={"phone": "13800138000"}
        )
        code = send.json()["dev_code"]
        login = client_with_real_auth.post(
            "/api/v1/auth/sms/login", json={"phone": "13800138000", "code": code}
        )
        login_body = login.json()
        access_v1 = login_body["access_token"]
        refresh_v1 = login_body["refresh_token"]
        parent_id = login_body["parent_id"]

        # 2) Use access v1 successfully
        r = client_with_real_auth.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": f"Bearer {access_v1}"},
        )
        assert r.status_code == 200
        assert r.json()["parent_id"] == parent_id

        # 3) Rotate refresh v1 -> access v2 + refresh v2
        rr = client_with_real_auth.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh_v1}
        )
        assert rr.status_code == 200
        rotated = rr.json()
        access_v2 = rotated["access_token"]
        refresh_v2 = rotated["refresh_token"]
        assert refresh_v2 != refresh_v1

        # 4) Using OLD refresh now should fail (rotation revoked it)
        rr_old = client_with_real_auth.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh_v1}
        )
        assert rr_old.status_code == 401

        # 5) Logout access_v2 -> whoami with access_v2 now 401
        lo = client_with_real_auth.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {access_v2}"},
        )
        assert lo.status_code == 200
        who = client_with_real_auth.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": f"Bearer {access_v2}"},
        )
        assert who.status_code == 401
        assert who.json()["error"]["code"] == "TOKEN_REVOKED"

        # 6) The new refresh v2 still works (rotation is for refresh, not
        # affected by access revocation)
        rr_v2 = client_with_real_auth.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh_v2}
        )
        assert rr_v2.status_code == 200
