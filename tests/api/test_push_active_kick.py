"""Tests for the BE-DEV-003 active-device + kick endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.main import create_app
from niki.db.repositories import (
    InMemoryPushDeviceRepository,
    set_push_device_repository,
)


@pytest.fixture
def client() -> TestClient:
    storage = InMemoryPushDeviceRepository()
    # Two devices for p-1, one disabled initially.
    storage._rows[("p-1", "d-1")] = {
        "id": "pd-1", "parent_id": "p-1", "device_id": "d-1",
        "channel": "apns", "push_token": "tok-1", "platform": "ios",
        "enabled": True, "last_active_at": "2026-07-30T00:00:00Z",
        "created_at": "2026-07-01T00:00:00Z",
    }
    storage._rows[("p-1", "d-2")] = {
        "id": "pd-2", "parent_id": "p-1", "device_id": "d-2",
        "channel": "hms", "push_token": "tok-2", "platform": "android",
        "enabled": False, "last_active_at": "2026-07-29T00:00:00Z",
        "created_at": "2026-07-01T00:00:00Z",
    }
    storage._rows[("p-2", "d-3")] = {
        "id": "pd-3", "parent_id": "p-2", "device_id": "d-3",
        "channel": "fcm", "push_token": "tok-3", "platform": "web",
        "enabled": True, "last_active_at": "2026-07-30T00:00:00Z",
        "created_at": "2026-07-01T00:00:00Z",
    }
    set_push_device_repository(storage)
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p-1", name="T",
        id_number_masked="****", phone_masked="****",
    )
    return TestClient(app)


class TestActiveDevices:
    def test_returns_only_enabled(
        self, client: TestClient
    ) -> None:
        r = client.get("/api/v1/push/active")
        assert r.status_code == 200
        body = r.json()
        # p-1 has 2 devices but d-2 is disabled → 1 active.
        assert body["count"] == 1
        assert body["devices"][0]["device_id"] == "d-1"

    def test_no_push_token(
        self, client: TestClient
    ) -> None:
        r = client.get("/api/v1/push/active")
        for d in r.json()["devices"]:
            assert "push_token" not in d

    def test_excludes_other_parents(
        self, client: TestClient
    ) -> None:
        r = client.get("/api/v1/push/active")
        device_ids = [d["device_id"] for d in r.json()["devices"]]
        assert "d-3" not in device_ids


class TestKick:
    def test_kick_disables_device(
        self, client: TestClient
    ) -> None:
        r = client.post("/api/v1/push/kick/d-1")
        assert r.status_code == 200
        assert r.json() == {"kicked": True}
        # d-1 should no longer be active.
        active = client.get("/api/v1/push/active").json()
        assert all(d["device_id"] != "d-1" for d in active["devices"])

    def test_kick_unknown_device(
        self, client: TestClient
    ) -> None:
        r = client.post("/api/v1/push/kick/d-never")
        assert r.status_code == 200
        assert r.json() == {"kicked": False}

    def test_kick_cannot_touch_other_parents_device(
        self, client: TestClient
    ) -> None:
        r = client.post("/api/v1/push/kick/d-3")
        # d-3 belongs to p-2; set_enabled filters by parent_id
        # → returns False.
        assert r.json() == {"kicked": False}

    def test_re_enable_brings_device_back(
        self, client: TestClient
    ) -> None:
        # Kick d-1
        client.post("/api/v1/push/kick/d-1")
        # Re-enable
        r = client.post("/api/v1/push/enable/d-1")
        assert r.status_code == 200
        assert r.json() == {"enabled": True}
        # d-1 is back in the active list.
        active = client.get("/api/v1/push/active").json()
        assert any(d["device_id"] == "d-1" for d in active["devices"])
