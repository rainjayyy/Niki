"""Tests for ISS-03: treehole purge must delete encrypted payloads too.

Updated for BE-STORG-001-ext-B (TreeholeManager now persists via
TreeholeRepository instead of an in-memory dict-of-lists).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from niki.db.repositories import (
    InMemoryTreeholeRepository,
    set_treehole_repository,
)
from niki.treehole.encryption import TreeholeEncryption
from niki.treehole.manager import TreeholeManager
from niki.treehole.purge import TreeholePurger


def _fresh_components() -> tuple[TreeholeManager, TreeholePurger]:
    """Create fresh (manager, purger) backed by a per-test in-memory repo.

    The repo is installed as the process-wide singleton so the default
    Purger constructor finds it.
    """
    repo = InMemoryTreeholeRepository()
    set_treehole_repository(repo)
    encryption = TreeholeEncryption(key=b"P" * 32)
    manager = TreeholeManager(encryption=encryption, retention_days=7, repo=repo)
    purger = TreeholePurger(retention_days=7, repo=repo)
    return manager, purger


def _backdate_row(row_id: str, repo: InMemoryTreeholeRepository, days_ago: int) -> None:
    """Make an existing row's expires_at be `days_ago` days in the past."""
    past = datetime.utcnow() - timedelta(days=days_ago)
    repo._rows[row_id]["expires_at"] = past


class TestTreeholePurgeEncryption:
    async def test_purge_removes_encrypted_payloads(self) -> None:
        """ISS-03: the old code matched `ts in ep.get("encrypted")` (base64
        ciphertext), which never matched. Encrypted payloads survived the
        7-day purge and grew unbounded. With the new index-based removal,
        they must be gone."""
        manager, purger = _fresh_components()
        repo = manager.repo

        # Add 2 old + 1 recent via the manager (so the encrypted bytes
        # are real AES-GCM output, not synthetic placeholders).
        for i in range(2):
            await manager.engage(
                parent_id="p1",
                child_id=f"old-{i}",
                original_text=f"old secret {i}",
                emotion="negative",
                intensity=3,
            )
        await manager.engage(
            parent_id="p1",
            child_id="new",
            original_text="recent secret",
            emotion="neutral",
            intensity=2,
        )

        # Sanity: 3 rows in the repo.
        assert len(repo._rows) == 3

        # Backdate the first 2 (oldest by created_at) so they look
        # expired. list_all filters by expires_at > now, so we check
        # _rows directly to assert pre-purge state.
        all_rows = sorted(
            [r for r in repo._rows.values()], key=lambda r: r["created_at"]
        )
        for r in all_rows[:2]:
            _backdate_row(r["id"], repo, days_ago=10)

        # Purge at "now"
        purged = await purger.purge_expired(now=datetime.utcnow())
        assert purged == 2

        # Only 1 row remains (the "new" one, still valid).
        remaining = await repo.list_all()
        assert len(remaining) == 1, "encrypted payloads survived the purge"
        assert remaining[0]["child_id"] == "new"

    async def test_purge_no_old_records_keeps_all(self) -> None:
        manager, purger = _fresh_components()
        for _ in range(3):
            await manager.engage(
                parent_id="p1",
                child_id=f"recent-{uuid.uuid4().hex[:8]}",
                original_text="recent",
                emotion="neutral",
                intensity=2,
            )
        purged = await purger.purge_expired(now=datetime.utcnow())
        assert purged == 0
        assert len(await manager.repo.list_all()) == 3

    async def test_purge_all_also_clears_encrypted(self) -> None:
        manager, purger = _fresh_components()
        for _ in range(3):
            await manager.engage(
                parent_id="p1",
                child_id="c",
                original_text="x",
                emotion="neutral",
                intensity=3,
            )
        purged = await purger.purge_all()
        assert purged == 3
        assert await manager.repo.list_all() == []
