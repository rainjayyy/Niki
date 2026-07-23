"""Tests for the treehole purge mechanism."""

from datetime import datetime, timedelta

import pytest

from niki.treehole.manager import TreeholeStore, TreeholeSummary
from niki.treehole.purge import TreeholePurger


class TestTreeholePurger:
    @pytest.fixture
    def purger(self) -> TreeholePurger:
        return TreeholePurger(retention_days=7)

    @pytest.fixture
    def store_with_old_records(self) -> TreeholeStore:
        store = TreeholeStore()
        now = datetime(2026, 7, 22)

        # Add 5 records: 3 old (>7 days), 2 new (<7 days)
        for i in range(3):
            old_ts = (now - timedelta(days=10 + i)).isoformat()
            s = TreeholeSummary(
                timestamp=old_ts,
                source="treehole",
                emotion_valence="negative",
                emotion_intensity=3,
            )
            store.add(s, {"child_id": f"old-{i}", "encrypted": "ct"})

        for i in range(2):
            new_ts = (now - timedelta(days=i + 1)).isoformat()
            s = TreeholeSummary(
                timestamp=new_ts,
                source="treehole",
                emotion_valence="neutral",
                emotion_intensity=2,
            )
            store.add(s, {"child_id": f"new-{i}", "encrypted": "ct"})

        return store

    async def test_purge_expired_removes_old_only(
        self,
        purger: TreeholePurger,
        store_with_old_records: TreeholeStore,
    ) -> None:
        now = datetime(2026, 7, 22)
        count = await purger.purge_expired(store_with_old_records, now=now)
        # 3 old records should be purged
        assert count == 3
        assert len(store_with_old_records.summaries) == 2

    async def test_purge_expired_no_old_records(
        self, purger: TreeholePurger
    ) -> None:
        store = TreeholeStore()
        now = datetime(2026, 7, 22)
        # Add a single recent record
        s = TreeholeSummary(
            timestamp=(now - timedelta(days=2)).isoformat(),
            source="treehole",
            emotion_valence="neutral",
            emotion_intensity=3,
        )
        store.add(s, {"child_id": "c1", "encrypted": "ct"})
        count = await purger.purge_expired(store, now=now)
        assert count == 0
        assert len(store.summaries) == 1

    async def test_purge_all_clears_store(
        self,
        purger: TreeholePurger,
        store_with_old_records: TreeholeStore,
    ) -> None:
        count = await purger.purge_all(store_with_old_records)
        assert count == 5
        assert len(store_with_old_records.summaries) == 0
        assert len(store_with_old_records.encrypted_payloads) == 0
        assert len(store_with_old_records.pending_purge) == 0

    async def test_purge_all_empty_store(
        self, purger: TreeholePurger
    ) -> None:
        store = TreeholeStore()
        count = await purger.purge_all(store)
        assert count == 0

    async def test_purge_expired_respects_retention_days(self) -> None:
        purger = TreeholePurger(retention_days=3)
        store = TreeholeStore()
        now = datetime(2026, 7, 22)
        # Add records at 2 days and 4 days old
        s_new = TreeholeSummary(
            timestamp=(now - timedelta(days=2)).isoformat(),
            source="treehole",
            emotion_valence="neutral",
            emotion_intensity=3,
        )
        s_old = TreeholeSummary(
            timestamp=(now - timedelta(days=4)).isoformat(),
            source="treehole",
            emotion_valence="neutral",
            emotion_intensity=3,
        )
        store.add(s_new, {"child_id": "c1", "encrypted": "ct"})
        store.add(s_old, {"child_id": "c2", "encrypted": "ct"})

        count = await purger.purge_expired(store, now=now)
        assert count == 1  # Only the 4-day-old record
        assert len(store.summaries) == 1

    async def test_purge_expired_default_retention_is_7_days(self) -> None:
        purger = TreeholePurger()
        assert purger.retention_days == 7
