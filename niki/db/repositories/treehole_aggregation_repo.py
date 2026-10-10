"""TreeholeAggregationRepository abstraction (BE-TREEHOLE-AGG, BE-TREEHOLE-AGG-rollout).

Holds per-child rolling aggregates of treehole engagements so the
baseline computation is O(1) instead of decrypting every stored
engagement on each dashboard refresh.

BE-TREEHOLE-AGG-rollout: each `increment()` updates BOTH
the daily and weekly windows in one call (cheap; the model
PK is `(child_id, window_kind, window_start)` so the two
rows coexist independently).

Two implementations:
- `InMemoryTreeholeAggregationRepository` — default for tests/dev.
- `PostgresTreeholeAggregationRepository` — production; uses
  INSERT ... ON CONFLICT DO UPDATE for atomic increments.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models import (
    TreeholeAggregation,
    WINDOW_KIND_DAILY,
    WINDOW_KIND_WEEKLY,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


VALENCE_WEIGHTS: dict[str, float] = {
    "positive": 0.5,
    "neutral": 0.0,
    "negative": -0.5,
}

VALID_CATEGORIES: tuple[str, ...] = ("family", "school", "self", "other")


def _valence_contribution(valence: str, intensity: int) -> float:
    return VALENCE_WEIGHTS.get(valence, 0.0) * (intensity / 5.0)


def _window_start_for(kind: str, ts: datetime) -> date:
    """Return the start date of the window that `ts` belongs to.

    - daily:  ts.date()
    - weekly: ts.date() - timedelta(days=ts.weekday())  (Mon)
    """
    if kind == WINDOW_KIND_DAILY:
        return ts.date()
    if kind == WINDOW_KIND_WEEKLY:
        # datetime.weekday(): Mon=0, Tue=1, ..., Sun=6.  Back to Monday.
        return ts.date() - timedelta(days=ts.weekday())
    raise ValueError(f"unknown window_kind: {kind!r}")


# ---------------------------------------------------------------------------
# Repository contract
# ---------------------------------------------------------------------------


class TreeholeAggregationRepository(ABC):
    """Contract for treehole aggregate persistence.

    BE-TREEHOLE-AGG-rollout: every method that touches a window
    accepts a `window_kind` parameter ("daily" | "weekly").
    The default is "weekly" for back-compat.
    """

    @abstractmethod
    async def increment(
        self,
        *,
        child_id: str,
        valence: str,
        intensity: int,
        category: str,
        engagement_at: datetime | None = None,
        window_start: date | None = None,
        window_kind: str = "all",
    ) -> dict[str, Any]:
        """Atomic upsert of a single engagement into the aggregate.

        Writes the row identified by
        (child_id, window_kind, window_start) and returns the
        updated row.

        Window selection:
            "daily"  → updates only the daily row
            "weekly" → updates only the weekly row
            "all"    → updates BOTH (the default, matches the
                       hot path used by `TreeholeManager.engage()`)
        """

    @abstractmethod
    async def get(
        self,
        child_id: str,
        window_kind: str = WINDOW_KIND_WEEKLY,
    ) -> dict[str, Any] | None:
        """Return the current aggregate for `child_id` (and
        `window_kind`) or None."""

    @abstractmethod
    async def list_all(
        self,
        window_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return every aggregate row. With `window_kind=None`
        (default) returns ALL windows; pass a kind to filter."""

    @abstractmethod
    async def reset(
        self,
        child_id: str,
        *,
        window_start: date,
        window_kind: str = WINDOW_KIND_WEEKLY,
    ) -> None:
        """Drop any existing aggregate row and (implicitly) let
        the next `increment()` start fresh."""

    @abstractmethod
    async def drop_all_for_child(self, child_id: str) -> int:
        """Drop every aggregate row for `child_id` across all
        window kinds. Returns count."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryTreeholeAggregationRepository(TreeholeAggregationRepository):
    """Thread-safe dict-backed aggregate store.

    Key: (child_id, window_kind, window_start_iso) -> row dict
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    async def _upsert_one(
        self,
        *,
        child_id: str,
        valence: str,
        intensity: int,
        category: str,
        engagement_at: datetime,
        window_start: date,
        window_kind: str,
    ) -> dict[str, Any]:
        contrib = _valence_contribution(valence, intensity)
        with self._lock:
            key = (child_id, window_kind, window_start.isoformat())
            row = self._rows.get(key)
            if row is None:
                row = {
                    "child_id": child_id,
                    "window_kind": window_kind,
                    "window_start": window_start,
                    "count": 0,
                    "valence_sum": 0.0,
                    "negative_count": 0,
                    "positive_count": 0,
                    "neutral_count": 0,
                    "intensity_sum": 0.0,
                    "category_counts": {},
                    "first_engagement_at": engagement_at,
                    "last_engagement_at": engagement_at,
                    "updated_at": engagement_at,
                }
                self._rows[key] = row
            row["count"] += 1
            row["valence_sum"] += contrib
            row["intensity_sum"] += intensity
            if valence == "positive":
                row["positive_count"] += 1
            elif valence == "negative":
                row["negative_count"] += 1
            else:
                row["neutral_count"] += 1
            cat_counts = dict(row["category_counts"])
            cat_counts[category] = cat_counts.get(category, 0) + 1
            row["category_counts"] = cat_counts
            if engagement_at < row["first_engagement_at"]:
                row["first_engagement_at"] = engagement_at
            if engagement_at > row["last_engagement_at"]:
                row["last_engagement_at"] = engagement_at
            row["updated_at"] = datetime.now(timezone.utc)
            return deepcopy(row)

    async def increment(
        self,
        *,
        child_id: str,
        valence: str,
        intensity: int,
        category: str,
        engagement_at: datetime | None = None,
        window_start: date | None = None,
        window_kind: str = "all",
    ) -> dict[str, Any]:
        ts = engagement_at or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # BE-TREEHOLE-AGG-rollout: when no explicit kind is
        # passed, fan out to BOTH daily and weekly so the
        # existing engage() call site works unchanged.
        if window_kind == "all" or window_kind is None:
            kinds: tuple[str, ...] = (WINDOW_KIND_DAILY, WINDOW_KIND_WEEKLY)
        else:
            kinds = (window_kind,)
        last: dict[str, Any] | None = None
        for k in kinds:
            ws = window_start or _window_start_for(k, ts)
            last = await self._upsert_one(
                child_id=child_id,
                valence=valence,
                intensity=intensity,
                category=category,
                engagement_at=ts,
                window_start=ws,
                window_kind=k,
            )
        # last can be None if kinds was empty (shouldn't happen with
        # the default "all" branch, but defensive). Return empty dict
        # to keep the declared contract (always returns dict, not None).
        return last or {}

    async def get(
        self,
        child_id: str,
        window_kind: str = WINDOW_KIND_WEEKLY,
    ) -> dict[str, Any] | None:
        """Return the most recent aggregate window of the
        given kind for `child_id`."""
        with self._lock:
            latest: tuple[str, dict[str, Any]] | None = None
            for (cid, kind, ws_iso), row in self._rows.items():
                if cid != child_id or kind != window_kind:
                    continue
                if latest is None or ws_iso > latest[0]:
                    latest = (ws_iso, row)
            return deepcopy(latest[1]) if latest else None

    async def list_all(self, window_kind: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if window_kind is None:
                return [deepcopy(row) for row in self._rows.values()]
            return [
                deepcopy(row) for (_, kind, _), row in self._rows.items() if kind == window_kind
            ]

    async def reset(
        self,
        child_id: str,
        *,
        window_start: date,
        window_kind: str = WINDOW_KIND_WEEKLY,
    ) -> None:
        with self._lock:
            self._rows.pop(
                (child_id, window_kind, window_start.isoformat()),
                None,
            )

    async def drop_all_for_child(self, child_id: str) -> int:
        with self._lock:
            to_drop = [k for k in self._rows if k[0] == child_id]
            for k in to_drop:
                self._rows.pop(k, None)
            return len(to_drop)

    def __len__(self) -> int:
        return len(self._rows)


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


def _row_to_dict(row: TreeholeAggregation) -> dict[str, Any]:
    return row.to_dict()


class PostgresTreeholeAggregationRepository(TreeholeAggregationRepository):
    """Atomic upsert via INSERT ... ON CONFLICT DO UPDATE."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _upsert_one(
        self,
        *,
        child_id: str,
        valence: str,
        intensity: int,
        category: str,
        engagement_at: datetime,
        window_start: date,
        window_kind: str,
    ) -> dict[str, Any]:
        contrib = _valence_contribution(valence, intensity)
        neg_inc = 1 if valence == "negative" else 0
        pos_inc = 1 if valence == "positive" else 0
        neu_inc = 1 if valence not in ("positive", "negative") else 0

        stmt = (
            pg_insert(TreeholeAggregation)
            .values(
                child_id=child_id,
                window_kind=window_kind,
                window_start=window_start,
                count=1,
                valence_sum=contrib,
                negative_count=neg_inc,
                positive_count=pos_inc,
                neutral_count=neu_inc,
                intensity_sum=intensity,
                category_counts={category: 1},
                first_engagement_at=engagement_at,
                last_engagement_at=engagement_at,
                updated_at=engagement_at,
            )
            .on_conflict_do_update(
                index_elements=[
                    TreeholeAggregation.child_id,
                    TreeholeAggregation.window_kind,
                    TreeholeAggregation.window_start,
                ],
                set_={
                    "count": TreeholeAggregation.count + 1,
                    "valence_sum": (TreeholeAggregation.valence_sum + contrib),
                    "intensity_sum": (TreeholeAggregation.intensity_sum + intensity),
                    "negative_count": (TreeholeAggregation.negative_count + neg_inc),
                    "positive_count": (TreeholeAggregation.positive_count + pos_inc),
                    "neutral_count": (TreeholeAggregation.neutral_count + neu_inc),
                    "last_engagement_at": engagement_at,
                    "updated_at": datetime.now(timezone.utc),
                    "category_counts": (TreeholeAggregation.category_counts),
                },
            )
            .returning(TreeholeAggregation)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one()
        await self._session.flush()
        return _row_to_dict(row)

    async def increment(
        self,
        *,
        child_id: str,
        valence: str,
        intensity: int,
        category: str,
        engagement_at: datetime | None = None,
        window_start: date | None = None,
        window_kind: str = "all",
    ) -> dict[str, Any]:
        ts = engagement_at or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if window_kind == "all" or window_kind is None:
            kinds: tuple[str, ...] = (WINDOW_KIND_DAILY, WINDOW_KIND_WEEKLY)
        else:
            kinds = (window_kind,)
        last: dict[str, Any] | None = None
        for k in kinds:
            ws = window_start or _window_start_for(k, ts)
            last = await self._upsert_one(
                child_id=child_id,
                valence=valence,
                intensity=intensity,
                category=category,
                engagement_at=ts,
                window_start=ws,
                window_kind=k,
            )
        # last can be None if kinds was empty (shouldn't happen with
        # the default "all" branch, but defensive). Return empty dict
        # to keep the declared contract (always returns dict, not None).
        return last or {}

    async def get(
        self,
        child_id: str,
        window_kind: str = WINDOW_KIND_WEEKLY,
    ) -> dict[str, Any] | None:
        result = await self._session.execute(
            select(TreeholeAggregation)
            .where(
                TreeholeAggregation.child_id == child_id,
                TreeholeAggregation.window_kind == window_kind,
            )
            .order_by(TreeholeAggregation.window_start.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return _row_to_dict(row) if row else None

    async def list_all(self, window_kind: str | None = None) -> list[dict[str, Any]]:
        stmt = select(TreeholeAggregation).order_by(
            TreeholeAggregation.child_id,
            TreeholeAggregation.window_kind,
            TreeholeAggregation.window_start,
        )
        if window_kind is not None:
            stmt = stmt.where(TreeholeAggregation.window_kind == window_kind)
        result = await self._session.execute(stmt)
        return [_row_to_dict(r) for r in result.scalars().all()]

    async def reset(
        self,
        child_id: str,
        *,
        window_start: date,
        window_kind: str = WINDOW_KIND_WEEKLY,
    ) -> None:
        from sqlalchemy import delete

        await self._session.execute(
            delete(TreeholeAggregation).where(
                TreeholeAggregation.child_id == child_id,
                TreeholeAggregation.window_kind == window_kind,
                TreeholeAggregation.window_start == window_start,
            )
        )
        await self._session.flush()

    async def drop_all_for_child(self, child_id: str) -> int:
        from sqlalchemy import delete

        result = await self._session.execute(
            delete(TreeholeAggregation).where(TreeholeAggregation.child_id == child_id)
        )
        await self._session.flush()
        return result.rowcount or 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_repo: TreeholeAggregationRepository | None = None


def get_treehole_aggregation_repository() -> TreeholeAggregationRepository:
    """Return the process-wide aggregate repo (default in-memory)."""
    global _repo
    if _repo is None:
        _repo = InMemoryTreeholeAggregationRepository()
    return _repo


def set_treehole_aggregation_repository(
    repo: TreeholeAggregationRepository,
) -> None:
    """Override the process-wide aggregate repo (tests / Postgres)."""
    global _repo
    _repo = repo
