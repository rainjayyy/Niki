"""Tests for the treehole purge mechanism (BE-STORG-001-ext-B).

Updated to use TreeholeRepository (in-memory) instead of the
legacy TreeholeStore shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from niki.db.repositories import (
    InMemoryTreeholeRepository,
    TreeholeRepository,
    set_treehole_repository,
)
from niki.treehole.encryption import TreeholeEncryption
from niki.treehole.manager import TreeholeManager
from niki.treehole.purge import TreeholePurger


def _make_manager_with_records(
    *,
    retention_days: int = 7,
    records: list[tuple[datetime, str, str]] | None = None,
) -> TreeholeManager:
    """Build a manager whose in-memory repo is pre-populated AND
    installed as the process-wide singleton so default-constructed
    purgers can find it."""
    repo = InMemoryTreeholeRepository()
    # Pre-populate by directly poking the dict (bypass async). The
    # InMemoryTreeholeRepository holds rows in self._rows; we
    # synthesize UUIDs and call utcnow() synchronously.
    import uuid as _uuid
    now = datetime.now(timezone.utc)
    for created_at, child_id, enc in records or []:
        repo._rows[str(_uuid.uuid4())] = {
            "id": str(_uuid.uuid4()),
            "child_id": child_id,
            "parent_id": "parent-1",
            "encrypted_payload": enc.encode("utf-8"),
            "associated_data": child_id.encode("utf-8"),
            "expires_at": created_at + timedelta(days=retention_days),
            "created_at": created_at,
        }
    set_treehole_repository(repo)
    return TreeholeManager(
        encryption=TreeholeEncryption(key=b"T" * 32),
        retention_days=retention_days,
        repo=repo,
    )


class TestTreeholePurger:
    def test_purger_default_retention_is_7_days(self) -> None:
        purger = TreeholePurger()
        assert purger.retention_days == 7

    async def test_purge_expired_removes_old_only(self) -> None:
        # retention=7d. created_at = T-10 → expires_at = T-3 (expired).
        # created_at = T-1  → expires_at = T+6 (valid).
        # Purging at now=2026-07-22 → exactly 3 removed, 2 remain.
        now = datetime(2026, 7, 22)
        old_records = [
            (now - timedelta(days=10), f"old-{i}", f"ct-old-{i}")
            for i in range(3)
        ]
        new_records = [
            (now - timedelta(days=1), f"new-{i}", f"ct-new-{i}")
            for i in range(2)
        ]
        mgr = _make_manager_with_records(
            records=old_records + new_records
        )
        purger = TreeholePurger(repo=mgr.repo)
        count = await purger.purge_expired(now=now)
        assert count == 3
        assert len(mgr.repo._rows) == 2

    async def test_purge_expired_no_old_records(self) -> None:
        now = datetime(2026, 7, 22)
        records = [(now - timedelta(days=2), "c1", "ct-1")]
        mgr = _make_manager_with_records(records=records)
        purger = TreeholePurger(repo=mgr.repo)
        # expires_at = now - 2d + 7d = now + 5d, so still valid
        count = await purger.purge_expired(now=now)
        assert count == 0

    async def test_purge_all_clears_repo(self) -> None:
        now = datetime(2026, 7, 22)
        records = [
            (now - timedelta(days=i + 1), f"c-{i}", f"ct-{i}")
            for i in range(5)
        ]
        mgr = _make_manager_with_records(records=records)
        purger = TreeholePurger(repo=mgr.repo)
        count = await purger.purge_all()
        assert count == 5
        remaining = await mgr.repo.list_all()
        assert len(remaining) == 0

    async def test_purge_all_empty_repo(self) -> None:
        mgr = _make_manager_with_records()
        purger = TreeholePurger(repo=mgr.repo)
        count = await purger.purge_all()
        assert count == 0

    async def test_purge_expired_respects_retention_days(self) -> None:
        # retention=3d. created_at = T-2 → expires_at = T+1 (valid).
        # created_at = T-4 → expires_at = T-1 (expired).
        now = datetime(2026, 7, 22)
        records = [
            (now - timedelta(days=2), "c-new", "ct-new"),
            (now - timedelta(days=4), "c-old", "ct-old"),
        ]
        mgr = _make_manager_with_records(
            retention_days=3, records=records
        )
        purger = TreeholePurger(repo=mgr.repo, retention_days=3)
        count = await purger.purge_expired(now=now)
        assert count == 1
        assert len(mgr.repo._rows) == 1


class TestTreeholePurgerAcceptsRepo:
    """Make sure TreeholePurger's repo= parameter is honored."""

    async def test_custom_repo_used(self) -> None:
        repo_a = InMemoryTreeholeRepository()
        repo_b = InMemoryTreeholeRepository()
        purger_a = TreeholePurger(retention_days=7, repo=repo_a)
        # If we explicitly pass repo_a, even after set_treehole_repository
        # swaps the default, purger_a still uses repo_a.
        from niki.db.repositories import set_treehole_repository
        set_treehole_repository(repo_b)
        # Insert via repo_a
        await repo_a.add(
            child_id="c",
            parent_id="p",
            encrypted_payload=b"x",
            associated_data=None,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        n = await purger_a.purge_expired()
        assert n == 1
        # repo_b should still be empty.
        assert await repo_b.list_all() == []
