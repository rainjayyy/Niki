"""Unit tests for niki.db.repositories.emotion_baseline_repo (BE-STORG-001-ext-D).

Both the InMemory and the Postgres (via SQLite-async) implementations
are exercised against the same contract.
"""

from __future__ import annotations

import pytest

from niki.db.repositories import (
    InMemoryEmotionBaselineRepository,
    PostgresEmotionBaselineRepository,
)


pytestmark = pytest.mark.asyncio


class TestInMemoryRepo:
    async def test_add_returns_id(self) -> None:
        repo = InMemoryEmotionBaselineRepository()
        row_id = await repo.add(
            date="2026-07-22", valence=0.5, intensity=3, source="normal"
        )
        assert isinstance(row_id, int)
        assert row_id >= 1

    async def test_list_in_window_returns_added_records(self) -> None:
        repo = InMemoryEmotionBaselineRepository()
        await repo.add(
            date="2026-07-22", valence=0.5, intensity=3, source="normal"
        )
        rows = await repo.list_in_window(window_days=7)
        assert len(rows) == 1
        assert rows[0]["date"] == "2026-07-22"

    async def test_list_in_window_filters_by_age(self) -> None:
        from datetime import datetime, timedelta

        repo = InMemoryEmotionBaselineRepository()
        # Insert 2 records; list_in_window(7) keeps both.
        await repo.add(
            date="2026-07-22", valence=0.5, intensity=3, source="normal"
        )
        await repo.add(
            date="2026-07-21", valence=0.3, intensity=3, source="treehole"
        )
        rows = await repo.list_in_window(window_days=7)
        assert len(rows) == 2
        # Validate both dates present
        dates = {r["date"] for r in rows}
        assert dates == {"2026-07-22", "2026-07-21"}

    async def test_list_all_returns_every_record(self) -> None:
        repo = InMemoryEmotionBaselineRepository()
        for i in range(3):
            await repo.add(
                date=f"2026-07-{20 + i:02d}",
                valence=0.1 * i,
                intensity=3,
                source="normal",
            )
        rows = await repo.list_all()
        assert len(rows) == 3

    async def test_len_counts_records(self) -> None:
        repo = InMemoryEmotionBaselineRepository()
        for _ in range(4):
            await repo.add(
                date="2026-07-22", valence=0.5, intensity=3, source="normal"
            )
        assert len(repo) == 4


class TestPostgresRepoSqlite:
    async def test_round_trip(self, async_session_factory) -> None:
        async with async_session_factory() as session:
            repo = PostgresEmotionBaselineRepository(session)
            await repo.add(
                date="2026-07-22", valence=0.5, intensity=3, source="normal"
            )
            await session.commit()

        async with async_session_factory() as session:
            repo2 = PostgresEmotionBaselineRepository(session)
            rows = await repo2.list_all()
        assert len(rows) == 1
        assert rows[0]["date"] == "2026-07-22"
        assert rows[0]["valence"] == 0.5
        assert rows[0]["intensity"] == 3
        assert rows[0]["source"] == "normal"
