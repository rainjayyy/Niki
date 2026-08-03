"""Integration tests for POST /api/v1/auth/sms/send + /sms/login (BE-AUTH-002)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import niki.api.auth as api_auth
from niki.auth.lifecycle import AccountLifecycle
from niki.auth.sms_service import (
    InMemorySMSCodeService,
    set_sms_service,
)
from niki.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Per-test isolated app + lifecycle + SMS service.

    Unset NIKI_TEST_FAKE_AUTH so the issued JWT works against /auth/whoami.
    """
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    api_auth._lifecycle = AccountLifecycle()
    set_sms_service(InMemorySMSCodeService())
    return TestClient(create_app())


def _send_payload(phone: str) -> dict[str, str]:
    return {"phone": phone}


def _login_payload(phone: str, code: str) -> dict[str, str]:
    return {"phone": phone, "code": code}


class TestSmsSend:
    def test_valid_phone_returns_dev_code(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/sms/send", json=_send_payload("13800138000"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["phone"] == "13800138000"
        assert body["status"] == "sent"
        # DEBUG mode echoes the code; production would omit dev_code.
        assert isinstance(body.get("dev_code"), str)
        assert len(body["dev_code"]) == 6

    def test_invalid_phone_returns_400(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/sms/send", json=_send_payload("not-a-phone"))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PHONE_FORMAT"

    def test_resend_within_ttl_returns_200(self, client: TestClient) -> None:
        """Within the active-code window a second send() is idempotent
        (returns same code, status 200). Cooldown only kicks in after
        the code is consumed (verified or expired)."""
        client.post("/api/v1/auth/sms/send", json=_send_payload("13800138000"))
        resp = client.post("/api/v1/auth/sms/send", json=_send_payload("13800138000"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"

    def test_cooldown_429_only_after_consume(self, client: TestClient) -> None:
        """Verify the code, then immediately re-send; cooldown should
        raise 429 because no active code is on file."""
        first = client.post(
            "/api/v1/auth/sms/send", json=_send_payload("13800138000")
        )
        code = first.json()["dev_code"]
        client.post(
            "/api/v1/auth/sms/login", json=_login_payload("13800138000", code)
        )
        resp = client.post("/api/v1/auth/sms/send", json=_send_payload("13800138000"))
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"


class TestSmsLogin:
    def test_valid_code_returns_jwt_pair(self, client: TestClient) -> None:
        send = client.post("/api/v1/auth/sms/send", json=_send_payload("13800138000"))
        code = send.json()["dev_code"]
        resp = client.post(
            "/api/v1/auth/sms/login", json=_login_payload("13800138000", code)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == 3600
        assert isinstance(body["parent_id"], str)

    def test_wrong_code_returns_401(self, client: TestClient) -> None:
        client.post("/api/v1/auth/sms/send", json=_send_payload("13800138000"))
        resp = client.post(
            "/api/v1/auth/sms/login", json=_login_payload("13800138000", "000000")
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "SMS_CODE_INVALID"

    def test_unknown_phone_returns_401(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/auth/sms/login", json=_login_payload("13800138000", "000000")
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "SMS_CODE_INVALID"

    def test_login_token_works_on_protected_endpoint(self, client: TestClient) -> None:
        send = client.post("/api/v1/auth/sms/send", json=_send_payload("13800138000"))
        code = send.json()["dev_code"]
        login = client.post(
            "/api/v1/auth/sms/login", json=_login_payload("13800138000", code)
        )
        token = login.json()["access_token"]
        # The `/auth/whoami` demo endpoint proves end-to-end auth wiring.
        who = client.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert who.status_code == 200
        assert who.json()["parent_id"] == login.json()["parent_id"]

    def test_login_auto_creates_pending_account(self, client: TestClient) -> None:
        send = client.post("/api/v1/auth/sms/send", json=_send_payload("13800138000"))
        code = send.json()["dev_code"]
        resp = client.post(
            "/api/v1/auth/sms/login", json=_login_payload("13800138000", code)
        )
        assert resp.status_code == 200
        assert resp.json()["account_status"] == "pending"

    def test_login_invalid_phone_format_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/auth/sms/login", json=_login_payload("bad", "000000")
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PHONE_FORMAT"

    def test_login_missing_code_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/auth/sms/login", json={"phone": "13800138000"}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "SMS_CODE_INVALID"

    def test_login_two_distinct_phones_get_distinct_accounts(
        self, client: TestClient
    ) -> None:
        s1 = client.post("/api/v1/auth/sms/send", json=_send_payload("13800138000"))
        s2 = client.post("/api/v1/auth/sms/send", json=_send_payload("13900139000"))
        l1 = client.post(
            "/api/v1/auth/sms/login",
            json=_login_payload("13800138000", s1.json()["dev_code"]),
        )
        l2 = client.post(
            "/api/v1/auth/sms/login",
            json=_login_payload("13900139000", s2.json()["dev_code"]),
        )
        assert l1.json()["parent_id"] != l2.json()["parent_id"]
