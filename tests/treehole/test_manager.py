"""Tests for the treehole manager (BE-STORG-001-ext-B).

Updated to use TreeholeRepository (in-memory) instead of the
legacy TreeholeStore shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from niki.db.repositories import (
    InMemoryTreeholeRepository,
    set_treehole_repository,
)
from niki.treehole.encryption import TreeholeEncryption
from niki.treehole.manager import TreeholeManager, TreeholeStore, TreeholeSummary


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> TreeholeManager:
    """Fresh in-memory repo per test."""
    repo = InMemoryTreeholeRepository()
    set_treehole_repository(repo)
    return TreeholeManager(encryption=TreeholeEncryption(key=b"T" * 32))


class TestTreeholeManager:
    async def test_engage_returns_summary_and_encrypted(
        self, manager: TreeholeManager
    ) -> None:
        summary, encrypted = await manager.engage(
            parent_id="parent-1",
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
        secret_text = "我有一个秘密不能让妈妈知道"
        await manager.engage(
            parent_id="parent-1",
            child_id="child-1",
            original_text=secret_text,
            emotion="negative",
            intensity=3,
        )
        # Persisted rows must not contain the secret text
        rows = await manager.repo.list_all()
        for r in rows:
            blob = r["encrypted_payload"]
            if isinstance(blob, bytes):
                blob = blob.decode("utf-8", errors="ignore")
            assert secret_text not in blob
            assert secret_text not in str(r.get("associated_data", b""))

    async def test_engage_persists_to_repo(
        self, manager: TreeholeManager
    ) -> None:
        await manager.engage(
            parent_id="parent-1",
            child_id="c1",
            original_text="secret",
            emotion="negative",
            intensity=3,
        )
        rows = await manager.repo.list_all()
        assert len(rows) == 1
        assert rows[0]["child_id"] == "c1"
        assert rows[0]["parent_id"] == "parent-1"

    async def test_engage_sets_7_day_expiry(
        self, manager: TreeholeManager
    ) -> None:
        await manager.engage(
            parent_id="parent-1",
            child_id="c1",
            original_text="secret",
            emotion="negative",
            intensity=3,
        )
        rows = await manager.repo.list_all()
        # expires_at should be ~7 days from now
        exp = rows[0]["expires_at"]
        delta = exp - datetime.now(timezone.utc)
        assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)

    async def test_decrypt_summary(self, manager: TreeholeManager) -> None:
        _, encrypted = await manager.engage(
            parent_id="parent-1",
            child_id="c1",
            original_text="secret",
            emotion="positive",
            intensity=5,
            category="school",
        )
        decrypted = await manager.decrypt_summary(encrypted)
        assert decrypted.emotion_valence == "positive"
        assert decrypted.emotion_intensity == 5
        assert decrypted.emotion_category == "school"

    async def test_aggregate_for_baseline(self, manager: TreeholeManager) -> None:
        # Add 3 negative-3, 2 positive-5
        for _ in range(3):
            await manager.engage(
                parent_id="p",
                child_id="c",
                original_text="a",
                emotion="negative",
                intensity=3,
            )
        for _ in range(2):
            await manager.engage(
                parent_id="p",
                child_id="c",
                original_text="b",
                emotion="positive",
                intensity=5,
            )
        result = await manager.aggregate_for_baseline()
        assert result["count"] == 5
        # 3 * (-0.5 * 3/5) + 2 * (0.5 * 5/5) = -0.9 + 1.0 = 0.1; mean = 0.02
        assert abs(result["mean_valence"] - 0.02) < 1e-6

    async def test_summary_has_no_text_field(
        self, manager: TreeholeManager
    ) -> None:
        summary, _ = await manager.engage(
            parent_id="p",
            child_id="c",
            original_text="my secret",
            emotion="neutral",
            intensity=3,
        )
        assert "text" not in summary.to_dict()
        assert "original" not in summary.to_dict()


class TestTreeholeStoreLegacyShape:
    """The legacy `TreeholeStore` class still exists for back-compat.

    These tests pin its shape so any future refactor that drops it
    fails loudly.
    """

    def test_initial_empty(self) -> None:
        store = TreeholeStore()
        assert store.summaries == []
        assert store.encrypted_payloads == []
        assert store.pending_purge == []

    def test_to_dict(self) -> None:
        store = TreeholeStore()
        d = store.to_dict()
        assert d == {
            "summary_count": 0,
            "encrypted_count": 0,
            "pending_purge_count": 0,
        }
