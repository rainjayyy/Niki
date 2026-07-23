"""Tests for the treehole manager."""


import pytest

from niki.treehole.encryption import TreeholeEncryption
from niki.treehole.manager import TreeholeManager, TreeholeStore, TreeholeSummary


@pytest.fixture
def manager() -> TreeholeManager:
    return TreeholeManager(encryption=TreeholeEncryption(key=b"T" * 32))


class TestTreeholeManager:
    async def test_engage_returns_summary_and_encrypted(
        self, manager: TreeholeManager
    ) -> None:
        summary, encrypted = await manager.engage(
            child_id="child-1",
            original_text="我有一个秘密",
            emotion="negative",
            intensity=4,
            category="family",
        )
        assert summary.source == "treehole"
        assert summary.emotion_valence == "negative"
        assert summary.emotion_intensity == 4
        assert summary.emotion_category == "family"
        assert "encrypted" in encrypted
        assert encrypted["child_id"] == "child-1"

    async def test_engage_does_not_store_original_text(
        self, manager: TreeholeManager
    ) -> None:
        """PRD #62: original text must NEVER be stored."""
        secret_text = "我偷偷告诉你一个秘密"
        await manager.engage(
            child_id="child-1",
            original_text=secret_text,
            emotion="negative",
            intensity=3,
        )
        # Original text must not appear anywhere in the store
        store_dump = str(manager.store.to_dict())
        assert secret_text not in store_dump
        # Also not in any summary
        for s in manager.store.summaries:
            assert secret_text not in str(s.to_dict())

    async def test_engage_schedules_7_day_purge(
        self, manager: TreeholeManager
    ) -> None:
        await manager.engage(
            child_id="c1",
            original_text="secret",
            emotion="negative",
            intensity=3,
        )
        assert len(manager.store.pending_purge) == 1
        # Default retention is 7 days
        assert manager.retention_days == 7

    async def test_decrypt_summary(self, manager: TreeholeManager) -> None:
        await manager.engage(
            child_id="c1",
            original_text="secret",
            emotion="positive",
            intensity=5,
            category="school",
        )
        summaries = await manager.list_summaries()
        assert len(summaries) == 1
        # Decrypt
        summary = await manager.decrypt_summary(
            manager.store.encrypted_payloads[0]
        )
        assert summary.source == "treehole"
        assert summary.emotion_valence == "positive"
        assert summary.emotion_intensity == 5
        assert summary.emotion_category == "school"

    async def test_aggregate_for_baseline(self, manager: TreeholeManager) -> None:
        """PRD #64: 聚合即匿名 — aggregate becomes irreversible."""
        # Multiple summaries
        for i in range(5):
            await manager.engage(
                child_id=f"c{i}",
                original_text=f"secret {i}",
                emotion="negative" if i % 2 == 0 else "positive",
                intensity=3,
            )

        agg = await manager.aggregate_for_baseline()
        assert "mean_valence" in agg
        assert agg["count"] == 5

    async def test_summary_has_no_text_field(self, manager: TreeholeManager) -> None:
        """The summary structure must not have a 'text' field."""
        summary, _ = await manager.engage(
            child_id="c1",
            original_text="some private content",
            emotion="neutral",
            intensity=3,
        )
        d = summary.to_dict()
        assert "text" not in d
        assert "original" not in d
        # The summary only contains structured fields
        assert set(d.keys()) == {
            "timestamp", "source", "emotion_valence",
            "emotion_intensity", "emotion_category", "z_score_deviation",
        }


class TestTreeholeSummary:
    def test_to_dict_roundtrip(self) -> None:
        s = TreeholeSummary(
            timestamp="2026-07-22T10:00:00",
            source="treehole",
            emotion_valence="negative",
            emotion_intensity=3,
            emotion_category="family",
        )
        d = s.to_dict()
        s2 = TreeholeSummary.from_dict(d)
        assert s2 == s


class TestTreeholeStore:
    def test_initial_empty(self) -> None:
        store = TreeholeStore()
        assert len(store.summaries) == 0

    def test_to_dict(self) -> None:
        store = TreeholeStore()
        d = store.to_dict()
        assert d["summary_count"] == 0
        assert d["encrypted_count"] == 0
        assert d["pending_purge_count"] == 0
