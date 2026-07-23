"""Tests for ISS-03: treehole purge must delete encrypted payloads too."""

import uuid
from datetime import datetime, timedelta

from niki.treehole.encryption import TreeholeEncryption
from niki.treehole.manager import TreeholeManager
from niki.treehole.purge import TreeholePurger


def _fresh_components():
    """Create fresh (manager, purger) for each test."""
    encryption = TreeholeEncryption(key=b"P" * 32)
    manager = TreeholeManager(encryption=encryption, retention_days=7)
    purger = TreeholePurger(retention_days=7)
    return manager, purger


class TestTreeholePurgeEncryption:
    async def test_purge_removes_encrypted_payloads(self) -> None:
        """ISS-03: the old code matched `ts in ep.get("encrypted")` (base64
        ciphertext), which never matched. Encrypted payloads survived the
        7-day purge and grew unbounded. With the new index-based removal,
        they must be gone."""
        manager, purger = _fresh_components()
        store = manager.store  # manager owns its store

        # Add 2 old (10 days ago) + 1 recent
        now = datetime(2026, 7, 22)
        for i in range(2):
            await manager.engage(
                child_id=f"old-{i}",
                original_text=f"old secret {i}",
                emotion="negative",
                intensity=3,
            )
            # Backdate the just-added records (engage() sets utcnow)
            store.summaries[-1] = store.summaries[-1].__class__(
                timestamp=(now - timedelta(days=10 + i)).isoformat(),
                source="treehole",
                emotion_valence="negative",
                emotion_intensity=3,
                emotion_category="other",
                z_score_deviation=None,
            )

        await manager.engage(
            child_id="new",
            original_text="recent secret",
            emotion="neutral",
            intensity=2,
        )

        # Sanity: 3 summaries, 3 encrypted_payloads
        assert len(store.summaries) == 3
        assert len(store.encrypted_payloads) == 3
        assert len(store.pending_purge) == 3

        # Purge
        purged = await purger.purge_expired(store, now=now)
        assert purged == 2

        # All three lists must shrink together
        assert len(store.summaries) == 1, "summaries not purged"
        assert len(store.encrypted_payloads) == 1, "ISS-03: encrypted payloads survived the purge"
        assert len(store.pending_purge) == 1

    async def test_purge_no_old_records_keeps_all(self) -> None:
        manager, purger = _fresh_components()
        store = manager.store
        now = datetime(2026, 7, 22)
        for _ in range(3):
            await manager.engage(
                child_id=f"recent-{uuid.uuid4().hex[:8]}",
                original_text="recent",
                emotion="neutral",
                intensity=2,
            )
        purged = await purger.purge_expired(store, now=now)
        assert purged == 0
        assert len(store.summaries) == 3
        assert len(store.encrypted_payloads) == 3

    async def test_purge_all_also_clears_encrypted(self) -> None:
        manager, purger = _fresh_components()
        store = manager.store
        for _ in range(3):
            await manager.engage(
                child_id="c",
                original_text="x",
                emotion="neutral",
                intensity=3,
            )
        purged = await purger.purge_all(store)
        assert purged == 3
        assert len(store.encrypted_payloads) == 0
        assert len(store.summaries) == 0
