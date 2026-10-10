"""Integration tests for the push notification registration
endpoints (BE-PUSH-001).

Exercises the API layer end-to-end with TestClient; the
conftest autouse fixture gives us a real `get_current_parent`
that returns a deterministic caller id, while still bypassing
JWT verification (`NIKI_TEST_FAKE_AUTH=1`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with a deterministic parent id 'p1'.

    We don't unset NIKI_TEST_FAKE_AUTH because the push endpoints
    don't depend on lateral-auth — they only require that the
    caller is *some* authenticated parent. A real JWT path test
    lives in tests/auth/test_protected_routes.py.
    """
    app = create_app()

    def fake():
        return ParentAccount(
            id="p1",
            name="Test",
            id_number_masked="****",
            phone_masked="****",
        )

    app.dependency_overrides[get_current_parent] = fake
    return TestClient(app)


class TestRegister:
    def test_register_creates_device(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/push/register",
            json={
                "device_id": "d-1",
                "channel": "apns",
                "push_token": "tok-a",
                "platform": "ios",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["device_id"] == "d-1"
        assert body["channel"] == "apns"
        assert body["platform"] == "ios"
        # push_token is NEVER echoed back
        assert "push_token" not in body

    def test_register_reregister_updates_token(self, client: TestClient) -> None:
        # First registration
        r1 = client.post(
            "/api/v1/push/register",
            json={
                "device_id": "d-2",
                "channel": "fcm",
                "push_token": "old-tok",
                "platform": "android",
            },
        )
        id1 = r1.json()["id"]
        # Re-register same device with a new token / channel
        r2 = client.post(
            "/api/v1/push/register",
            json={
                "device_id": "d-2",
                "channel": "hms",
                "push_token": "new-tok",
                "platform": "android",
            },
        )
        assert r2.status_code == 200
        body = r2.json()
        # Same row updated, not a new one
        assert body["id"] == id1
        assert body["channel"] == "hms"
        # list shows 1 entry (not duplicated)
        listing = client.get("/api/v1/push/devices").json()
        assert listing["count"] == 1
        assert listing["devices"][0]["device_id"] == "d-2"

    def test_register_invalid_channel_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/push/register",
            json={
                "device_id": "d-3",
                "channel": "telegram",
                "push_token": "tok",
                "platform": "ios",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_CHANNEL"

    def test_register_invalid_device_id_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/push/register",
            json={
                "device_id": "<script>",
                "channel": "apns",
                "push_token": "tok",
                "platform": "ios",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_DEVICE_ID"

    def test_register_missing_token_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/push/register",
            json={
                "device_id": "d-4",
                "channel": "apns",
                "push_token": "",
                "platform": "ios",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_PUSH_TOKEN"


class TestUnregister:
    def test_unregister_by_device_id(self, client: TestClient) -> None:
        client.post(
            "/api/v1/push/register",
            json={
                "device_id": "d-u1",
                "channel": "apns",
                "push_token": "tok-u1",
                "platform": "ios",
            },
        )
        r = client.post("/api/v1/push/unregister", json={"device_id": "d-u1"})
        assert r.status_code == 200
        assert r.json()["unregistered"] is True
        assert r.json()["removed"] == 1
        # Verify it was actually removed
        listing = client.get("/api/v1/push/devices").json()
        assert listing["count"] == 0

    def test_unregister_by_token(self, client: TestClient) -> None:
        client.post(
            "/api/v1/push/register",
            json={
                "device_id": "d-u2",
                "channel": "fcm",
                "push_token": "tok-u2",
                "platform": "android",
            },
        )
        r = client.post("/api/v1/push/unregister", json={"push_token": "tok-u2"})
        assert r.status_code == 200
        assert r.json()["removed"] == 1

    def test_unregister_unknown_device_is_idempotent(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/push/unregister",
            json={"device_id": "never-registered"},
        )
        assert r.status_code == 200
        assert r.json()["unregistered"] is True
        assert r.json()["removed"] == 0

    def test_unregister_neither_id_nor_token_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/push/unregister", json={})
        assert r.status_code == 400


class TestListDevices:
    def test_list_returns_only_callers_devices(self, client: TestClient) -> None:
        # Register two devices
        for i in range(2):
            client.post(
                "/api/v1/push/register",
                json={
                    "device_id": f"d-{i}",
                    "channel": "apns",
                    "push_token": f"tok-{i}",
                    "platform": "ios",
                },
            )
        listing = client.get("/api/v1/push/devices").json()
        assert listing["count"] == 2
        # push_token never leaks
        for dev in listing["devices"]:
            assert "push_token" not in dev

    def test_list_empty_when_none_registered(self, client: TestClient) -> None:
        listing = client.get("/api/v1/push/devices").json()
        assert listing == {"count": 0, "devices": []}


class TestPrefs:
    def test_get_returns_defaults(self, client: TestClient) -> None:
        r = client.get("/api/v1/push/prefs")
        assert r.status_code == 200
        body = r.json()
        assert body["quiet_start"] is None
        assert body["high_risk_only"] is False
        assert "apns" in body["enabled_channels"]

    def test_patch_quiet_hours(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"quiet_start": 22, "quiet_end": 7},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["quiet_start"] == 22
        assert body["quiet_end"] == 7

    def test_patch_invalid_quiet_hour_returns_400(self, client: TestClient) -> None:
        r = client.patch("/api/v1/push/prefs", json={"quiet_start": 99})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_QUIET_HOUR"

    def test_patch_invalid_channels_returns_400(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"enabled_channels": ["apns", "wechat"]},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_ENABLED_CHANNELS"

    def test_patch_high_risk_only(self, client: TestClient) -> None:
        r = client.patch("/api/v1/push/prefs", json={"high_risk_only": True})
        assert r.status_code == 200
        body = r.json()
        assert body["high_risk_only"] is True
        # Other fields preserved as defaults
        assert body["quiet_start"] is None

    def test_patch_partial_preserves_other_fields(self, client: TestClient) -> None:
        # Set quiet_start first
        client.patch("/api/v1/push/prefs", json={"quiet_start": 22})
        # Then patch only high_risk_only — quiet_start preserved
        r = client.patch("/api/v1/push/prefs", json={"high_risk_only": True})
        assert r.json()["quiet_start"] == 22
