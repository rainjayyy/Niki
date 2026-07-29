"""Tests for the daily + weekly window rollout (BE-TREEHOLE-AGG-rollout)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from niki.db.models import (
    WINDOW_KIND_DAILY,
    WINDOW_KIND_WEEKLY,
)
from niki.db.repositories import (
    InMemoryTreeholeAggregationRepository,
)
from niki.db.repositories.treehole_aggregation_repo import (
    _window_start_for,
)


# ---------------------------------------------------------------------------
# Window boundary
# ---------------------------------------------------------------------------


class TestWindowStartFor:
    def test_daily_returns_same_date(self) -> None:
        ts = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
        assert _window_start_for(WINDOW_KIND_DAILY, ts) == date(2026, 7, 29)

    def test_weekly_returns_monday(self) -> None:
        # 2026-07-29 is a Wednesday.
        ts = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
        # ISO weekday: Wed=3 → back 2 days to Mon=1.
        assert _window_start_for(WINDOW_KIND_WEEKLY, ts) == date(
            2026, 7, 27
        )

    def test_weekly_sunday_returns_monday(self) -> None:
        # 2026-08-02 is a Sunday.
        ts = datetime(2026, 8, 2, 23, 0, tzinfo=timezone.utc)
        assert _window_start_for(WINDOW_KIND_WEEKLY, ts) == date(
            2026, 7, 27
        )

    def test_weekly_monday_returns_same_monday(self) -> None:
        # 2026-07-27 is a Monday.
        ts = datetime(2026, 7, 27, 0, 1, tzinfo=timezone.utc)
        assert _window_start_for(WINDOW_KIND_WEEKLY, ts) == date(
            2026, 7, 27
        )

    def test_unknown_window_raises(self) -> None:
        ts = datetime(2026, 7, 29, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            _window_start_for("monthly", ts)


# ---------------------------------------------------------------------------
# InMemoryTreeholeAggregationRepository — multi-window increment
# ---------------------------------------------------------------------------


class TestInMemoryMultiWindow:
    async def test_default_increment_writes_both_windows(self) -> None:
        """Calling `increment()` with no window_kind writes to
        both daily and weekly. This is the hot path used by
        `TreeholeManager.engage()`."""
        repo = InMemoryTreeholeAggregationRepository()
        ts = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
        last = await repo.increment(
            child_id="c-1",
            valence="negative",
            intensity=4,
            category="family",
            engagement_at=ts,
        )
        # Two rows should now exist: daily and weekly.
        assert len(repo) == 2
        # The returned row is the most recently updated.
        # We just check that it has a window_kind set.
        assert last["window_kind"] in (
            WINDOW_KIND_DAILY, WINDOW_KIND_WEEKLY
        )

        # Both windows have count == 1.
        daily = await repo.get("c-1", window_kind=WINDOW_KIND_DAILY)
        weekly = await repo.get("c-1", window_kind=WINDOW_KIND_WEEKLY)
        assert daily["count"] == 1
        assert weekly["count"] == 1
        assert daily["count"] == weekly["count"]

    async def test_explicit_window_kind_writes_one_window(self) -> None:
        """Passing window_kind='daily' writes only the daily row."""
        repo = InMemoryTreeholeAggregationRepository()
        ts = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
        await repo.increment(
            child_id="c-1",
            valence="negative",
            intensity=4,
            category="family",
            engagement_at=ts,
            window_kind=WINDOW_KIND_DAILY,
        )
        assert len(repo) == 1
        daily = await repo.get("c-1", window_kind=WINDOW_KIND_DAILY)
        weekly = await repo.get("c-1", window_kind=WINDOW_KIND_WEEKLY)
        assert daily is not None and daily["count"] == 1
        assert weekly is None

    async def test_cross_window_independence(self) -> None:
        """Daily and weekly aggregates don't share counts."""
        repo = InMemoryTreeholeAggregationRepository()
        ts = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
        # 5 daily events
        for _ in range(5):
            await repo.increment(
                child_id="c-1", valence="neutral", intensity=3,
                category="other", engagement_at=ts,
                window_kind=WINDOW_KIND_DAILY,
            )
        # 2 weekly events
        for _ in range(2):
            await repo.increment(
                child_id="c-1", valence="neutral", intensity=3,
                category="other", engagement_at=ts,
                window_kind=WINDOW_KIND_WEEKLY,
            )
        daily = await repo.get("c-1", window_kind=WINDOW_KIND_DAILY)
        weekly = await repo.get("c-1", window_kind=WINDOW_KIND_WEEKLY)
        assert daily["count"] == 5
        assert weekly["count"] == 2

    async def test_different_window_starts(self) -> None:
        """Window start depends on kind: daily = engagement
        date, weekly = Monday of that week."""
        repo = InMemoryTreeholeAggregationRepository()
        # Wednesday July 29
        ts_wed = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
        # Sunday August 2 (same week)
        ts_sun = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
        # Use explicit "all" so both windows are written.
        await repo.increment(
            child_id="c-1", valence="neutral", intensity=3,
            category="self", engagement_at=ts_wed,
            window_kind="all",
        )
        await repo.increment(
            child_id="c-1", valence="neutral", intensity=3,
            category="self", engagement_at=ts_sun,
            window_kind="all",
        )
        # Daily: 2 separate windows (Jul 29 + Aug 2)
        # Weekly: 1 window (both in the same week)
        # Total: 3 rows
        assert len(repo) == 3
        # Weekly: both in the same week (Mon Jul 27)
        weekly = await repo.get("c-1", window_kind=WINDOW_KIND_WEEKLY)
        assert weekly is not None
        assert weekly["count"] == 2
        assert weekly["window_start"] == date(2026, 7, 27)

    async def test_get_isolates_windows(self) -> None:
        """`get(child)` with no window returns the most recent
        of any kind; with window_kind it filters to that kind."""
        repo = InMemoryTreeholeAggregationRepository()
        ts = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
        await repo.increment(
            child_id="c-1", valence="neutral", intensity=3,
            category="other", engagement_at=ts,
            window_kind=WINDOW_KIND_DAILY,
        )
        # Asking for the weekly kind returns None (no weekly row)
        assert await repo.get("c-1", window_kind=WINDOW_KIND_WEEKLY) is None
        # Asking for daily returns the daily row
        daily = await repo.get("c-1", window_kind=WINDOW_KIND_DAILY)
        assert daily is not None

    async def test_list_all_with_window_filter(self) -> None:
        repo = InMemoryTreeholeAggregationRepository()
        ts = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
        await repo.increment(
            child_id="c-1", valence="neutral", intensity=3,
            category="other", engagement_at=ts,
            window_kind=WINDOW_KIND_DAILY,
        )
        await repo.increment(
            child_id="c-1", valence="neutral", intensity=3,
            category="other", engagement_at=ts,
            window_kind=WINDOW_KIND_WEEKLY,
        )
        # No filter → 2 rows
        assert len(await repo.list_all()) == 2
        # Filtered → 1 each
        assert len(await repo.list_all(window_kind=WINDOW_KIND_DAILY)) == 1
        assert len(await repo.list_all(window_kind=WINDOW_KIND_WEEKLY)) == 1

    async def test_drop_all_for_child_clears_both(self) -> None:
        repo = InMemoryTreeholeAggregationRepository()
        ts = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
        await repo.increment(
            child_id="c-1", valence="neutral", intensity=3,
            category="other", engagement_at=ts,
        )
        assert len(repo) == 2
        dropped = await repo.drop_all_for_child("c-1")
        assert dropped == 2
        assert len(repo) == 0


# ---------------------------------------------------------------------------
# Manager integration
# ---------------------------------------------------------------------------


class TestManagerWindowKind:
    async def test_aggregate_weekly_default(self) -> None:
        from niki.treehole.manager import TreeholeManager
        from niki.treehole.encryption import TreeholeEncryption
        mgr = TreeholeManager(encryption=TreeholeEncryption(key=b"T" * 32))
        for _ in range(3):
            await mgr.engage(
                parent_id="p-1", child_id="c-1",
                original_text="x", emotion="negative",
                intensity=4,
            )
        # Default: weekly
        result = await mgr.aggregate_for_baseline(child_id="c-1")
        assert result["count"] == 3
        # Explicit weekly
        result_w = await mgr.aggregate_for_baseline(
            child_id="c-1", window_kind="weekly"
        )
        assert result_w["count"] == 3

    async def test_aggregate_daily(self) -> None:
        from niki.treehole.manager import TreeholeManager
        from niki.treehole.encryption import TreeholeEncryption
        mgr = TreeholeManager(encryption=TreeholeEncryption(key=b"T" * 32))
        for _ in range(5):
            await mgr.engage(
                parent_id="p-1", child_id="c-1",
                original_text="x", emotion="neutral",
                intensity=3,
            )
        result = await mgr.aggregate_for_baseline(
            child_id="c-1", window_kind="daily"
        )
        assert result["count"] == 5

    async def test_daily_and_weekly_isolated(self) -> None:
        """Same engagement flow updates both windows but each
        has its own count."""
        from niki.treehole.manager import TreeholeManager
        from niki.treehole.encryption import TreeholeEncryption
        mgr = TreeholeManager(encryption=TreeholeEncryption(key=b"T" * 32))
        # 2 negative-3
        for _ in range(2):
            await mgr.engage(
                parent_id="p-1", child_id="c-1",
                original_text="x", emotion="negative", intensity=3,
            )
        # 3 positive-5
        for _ in range(3):
            await mgr.engage(
                parent_id="p-1", child_id="c-1",
                original_text="y", emotion="positive", intensity=5,
            )
        # Both windows have count=5
        d = await mgr.aggregate_for_baseline(
            child_id="c-1", window_kind="daily"
        )
        w = await mgr.aggregate_for_baseline(
            child_id="c-1", window_kind="weekly"
        )
        assert d["count"] == 5
        assert w["count"] == 5
        # Mean identical (same data)
        assert d["mean_valence"] == w["mean_valence"]