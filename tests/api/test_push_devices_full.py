"""Tests for the device-management endpoints (BE-DEV-002)."""

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
    # Seed two devices for parent p-1.
    storage._rows[("p-1", "d-1")] = {
        "id": "pd-1",
        "parent_id": "p-1",
        "device_id": "d-1",
        "channel": "apns",
        "push_token": "tok-secret-1",
        "platform": "ios",
        "enabled": True,
        "last_active_at": "2026-07-30T00:00:00Z",
        "created_at": "2026-07-01T00:00:00Z",
    }
    storage._rows[("p-1", "d-2")] = {
        "id": "pd-2",
        "parent_id": "p-1",
        "device_id": "d-2",
        "channel": "hms",
        "push_token": "tok-secret-2",
        "platform": "android",
        "enabled": True,
        "last_active_at": "2026-07-30T00:00:00Z",
        "created_at": "2026-07-01T00:00:00Z",
    }
    # One device for a different parent.
    storage._rows[("p-2", "d-3")] = {
        "id": "pd-3",
        "parent_id": "p-2",
        "device_id": "d-3",
        "channel": "fcm",
        "push_token": "tok-secret-3",
        "platform": "web",
        "enabled": True,
        "last_active_at": "2026-07-30T00:00:00Z",
        "created_at": "2026-07-01T00:00:00Z",
    }
    set_push_device_repository(storage)
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p-1", name="T",
        id_number_masked="****", phone_masked="****",
    )
    return TestClient(app)


def test_list_returns_only_callers_devices(client: TestClient) -> None:
    r = client.get("/api/v1/push/devices-full")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    # No push_token ever returned.
    for d in body["devices"]:
        assert "push_token" not in d


def test_list_excludes_other_parents_device(client: TestClient) -> None:
    r = client.get("/api/v1/push/devices-full")
    body = r.json()
    device_ids = [d["device_id"] for d in body["devices"]]
    assert "d-1" in device_ids
    assert "d-2" in device_ids
    assert "d-3" not in device_ids  # belongs to p-2


def test_unregister_removes_device(client: TestClient) -> None:
    r = client.delete("/api/v1/push/devices-full/d-1")
    assert r.status_code == 200
    body = r.json()
    assert body["unregistered"] is True
    assert body["removed"] == 1
    # d-1 is now gone from the list.
    listing = client.get("/api/v1/push/devices-full").json()
    assert listing["count"] == 1
    assert listing["devices"][0]["device_id"] == "d-2"


def test_unregister_unknown_device_is_idempotent(
    client: TestClient,
) -> None:
    r = client.delete("/api/v1/push/devices-full/d-never")
    assert r.status_code == 200
    assert r.json()["removed"] == 0


def test_unregister_cannot_touch_other_parents_device(
    client: TestClient,
) -> None:
    """A parent deleting 'd-3' (which belongs to p-2) should
    see removed=0 because delete_by_device_id filters by
    parent_id."""
    r = client.delete("/api/v1/push/devices-full/d-3")
    assert r.json()["removed"] == 0
