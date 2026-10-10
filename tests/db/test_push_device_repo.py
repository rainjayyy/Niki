"""Tests for the push device + prefs repositories (BE-PUSH-001)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from niki.db.repositories.push_device_repo import (
    InMemoryPushDeviceRepository,
    InMemoryPushPrefsRepository,
    default_prefs,
)


class TestInMemoryPushDeviceRepository:
    async def test_upsert_creates_row(self) -> None:
        repo = InMemoryPushDeviceRepository()
        row = await repo.upsert(
            parent_id="p1",
            device_id="d-1",
            channel="apns",
            push_token="token-a",
            platform="ios",
        )
        assert row["parent_id"] == "p1"
        assert row["device_id"] == "d-1"
        assert row["channel"] == "apns"
        assert row["platform"] == "ios"
        assert row["enabled"] is True

    async def test_upsert_existing_updates_in_place(self) -> None:
        repo = InMemoryPushDeviceRepository()
        row1 = await repo.upsert(
            parent_id="p1",
            device_id="d-1",
            channel="apns",
            push_token="token-a",
            platform="ios",
        )
        original_id = row1["id"]
        original_created = row1["created_at"]

        row2 = await repo.upsert(
            parent_id="p1",
            device_id="d-1",
            channel="fcm",
            push_token="token-b",
            platform="android",
        )
        # id + created_at preserved
        assert row2["id"] == original_id
        assert row2["created_at"] == original_created
        # mutable fields replaced
        assert row2["channel"] == "fcm"
        assert row2["push_token"] == "token-b"
        assert row2["platform"] == "android"

    async def test_list_for_parent_isolated(self) -> None:
        repo = InMemoryPushDeviceRepository()
        await repo.upsert(
            parent_id="p1",
            device_id="d-1",
            channel="apns",
            push_token="t-a",
            platform="ios",
        )
        await repo.upsert(
            parent_id="p1",
            device_id="d-2",
            channel="hms",
            push_token="t-b",
            platform="android",
        )
        await repo.upsert(
            parent_id="p2",
            device_id="d-1",
            channel="fcm",
            push_token="t-c",
            platform="ios",
        )
        rows = await repo.list_for_parent("p1")
        assert len(rows) == 2
        rows2 = await repo.list_for_parent("p2")
        assert len(rows2) == 1

    async def test_delete_by_device_id(self) -> None:
        repo = InMemoryPushDeviceRepository()
        await repo.upsert(
            parent_id="p1",
            device_id="d-1",
            channel="apns",
            push_token="t-a",
            platform="ios",
        )
        removed = await repo.delete_by_device_id("p1", "d-1")
        assert removed == 1
        assert await repo.list_for_parent("p1") == []
        # Idempotent
        removed2 = await repo.delete_by_device_id("p1", "d-1")
        assert removed2 == 0

    async def test_delete_by_token(self) -> None:
        repo = InMemoryPushDeviceRepository()
        await repo.upsert(
            parent_id="p1",
            device_id="d-1",
            channel="apns",
            push_token="t-a",
            platform="ios",
        )
        removed = await repo.delete_by_token("t-a")
        assert removed == 1
        assert await repo.list_for_parent("p1") == []

    async def test_get_by_token(self) -> None:
        repo = InMemoryPushDeviceRepository()
        await repo.upsert(
            parent_id="p1",
            device_id="d-1",
            channel="apns",
            push_token="t-a",
            platform="ios",
        )
        row = await repo.get_by_token("t-a")
        assert row is not None
        assert row["device_id"] == "d-1"
        assert await repo.get_by_token("nonexistent") is None


class TestInMemoryPushPrefsRepository:
    async def test_get_returns_defaults_for_new_parent(self) -> None:
        repo = InMemoryPushPrefsRepository()
        prefs = await repo.get("p1")
        assert prefs["parent_id"] == "p1"
        assert prefs["quiet_start"] is None
        assert prefs["quiet_end"] is None
        assert "hms" in prefs["enabled_channels"]
        assert "apns" in prefs["enabled_channels"]
        assert prefs["high_risk_only"] is False

    async def test_update_partial(self) -> None:
        repo = InMemoryPushPrefsRepository()
        # First update creates the row with the partial fields
        await repo.update(
            "p1",
            quiet_start=22,
            quiet_end=7,
        )
        prefs = await repo.get("p1")
        assert prefs["quiet_start"] == 22
        assert prefs["quiet_end"] == 7
        assert "hms" in prefs["enabled_channels"]  # default preserved

        # Second update touches a different field — earlier ones stay
        await repo.update("p1", high_risk_only=True)
        prefs2 = await repo.get("p1")
        assert prefs2["quiet_start"] == 22  # preserved
        assert prefs2["quiet_end"] == 7  # preserved
        assert prefs2["high_risk_only"] is True  # updated

    async def test_update_enabled_channels(self) -> None:
        repo = InMemoryPushPrefsRepository()
        await repo.update("p1", enabled_channels=["apns", "web"])
        prefs = await repo.get("p1")
        assert prefs["enabled_channels"] == ["apns", "web"]

    async def test_isolated_per_parent(self) -> None:
        repo = InMemoryPushPrefsRepository()
        await repo.update("p1", quiet_start=22)
        await repo.update("p2", quiet_start=8)
        assert (await repo.get("p1"))["quiet_start"] == 22
        assert (await repo.get("p2"))["quiet_start"] == 8


def test_default_prefs_isolated() -> None:
    """Mutating one default-prefs dict must NOT affect another."""
    a = default_prefs("p1")
    a["enabled_channels"].append("zzz")
    b = default_prefs("p2")
    assert "zzz" not in b["enabled_channels"]
