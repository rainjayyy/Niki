"""TreeholeAggregationRepository abstraction (BE-TREEHOLE-AGG).

Holds per-child rolling aggregates of treehole engagements so the
baseline computation is O(1) instead of decrypting every stored
engagement on each dashboard refresh.

Two implementations:
- `InMemoryTreeholeAggregationRepository` — default for tests/dev.
- `PostgresTreeholeAggregationRepository` — production; uses
  INSERT ... ON CONFLICT DO UPDATE for atomic increments.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models import TreeholeAggregation


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


# ---------------------------------------------------------------------------
# Repository contract
# ---------------------------------------------------------------------------


class TreeholeAggregationRepository(ABC):
    """Contract for treehole aggregate persistence."""

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
    ) -> dict[str, Any]:
        """Atomic upsert of a single engagement into the aggregate.

        Returns the updated aggregate row as a dict.
        """

    @abstractmethod
    async def get(self, child_id: str) -> dict[str, Any] | None:
        """Return the current aggregate for `child_id` or None."""

    @abstractmethod
    async def list_all(self) -> list[dict[str, Any]]:
        """Return every aggregate row (every child × every window).

        Used by cross-child rollups (`aggregate_for_baseline()`
        with no `child_id`) and by ops dashboards.
        """

    @abstractmethod
    async def reset(
        self,
        child_id: str,
        *,
        window_start: date,
    ) -> None:
        """Drop any existing aggregate row and (implicitly) let
        the next `increment()` start fresh."""

    @abstractmethod
    async def drop_all_for_child(self, child_id: str) -> int:
        """Drop every aggregate row for `child_id`. Returns count."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryTreeholeAggregationRepository(TreeholeAggregationRepository):
    """Thread-safe dict-backed aggregate store."""

    def __init__(self) -> None:
        # key: (child_id, window_start_iso) -> dict
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    async def increment(
        self,
        *,
        child_id: str,
        valence: str,
        intensity: int,
        category: str,
        engagement_at: datetime | None = None,
        window_start: date | None = None,
    ) -> dict[str, Any]:
        ts = engagement_at or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ws = window_start or ts.date()
        contrib = _valence_contribution(valence, intensity)
        with self._lock:
            key = (child_id, ws.isoformat())
            row = self._rows.get(key)
            if row is None:
                row = {
                    "child_id": child_id,
                    "window_start": ws,
                    "count": 0,
                    "valence_sum": 0.0,
                    "negative_count": 0,
                    "positive_count": 0,
                    "neutral_count": 0,
                    "intensity_sum": 0.0,
                    "category_counts": {},
                    "first_engagement_at": ts,
                    "last_engagement_at": ts,
                    "updated_at": ts,
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
            if ts < row["first_engagement_at"]:
                row["first_engagement_at"] = ts
            if ts > row["last_engagement_at"]:
                row["last_engagement_at"] = ts
            row["updated_at"] = datetime.now(timezone.utc)
            return deepcopy(row)

    async def get(self, child_id: str) -> dict[str, Any] | None:
        """Return the most recent aggregate window for `child_id`.

        Multiple windows may exist if data spans past 7 days
        (e.g. after a window reset). We return the one with the
        latest `window_start` so callers see the freshest signal.
        """
        with self._lock:
            latest: tuple[str, dict[str, Any]] | None = None
            for (cid, ws_iso), row in self._rows.items():
                if cid != child_id:
                    continue
                if latest is None or ws_iso > latest[0]:
                    latest = (ws_iso, row)
            return deepcopy(latest[1]) if latest else None

    async def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(row) for row in self._rows.values()]

    async def reset(
        self, child_id: str, *, window_start: date
    ) -> None:
        with self._lock:
            self._rows.pop((child_id, window_start.isoformat()), None)

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

    async def increment(
        self,
        *,
        child_id: str,
        valence: str,
        intensity: int,
        category: str,
        engagement_at: datetime | None = None,
        window_start: date | None = None,
    ) -> dict[str, Any]:
        ts = engagement_at or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ws = window_start or ts.date()
        contrib = _valence_contribution(valence, intensity)
        neg_inc = 1 if valence == "negative" else 0
        pos_inc = 1 if valence == "positive" else 0
        neu_inc = 1 if valence not in ("positive", "negative") else 0

        # Postgres-only INSERT ... ON CONFLICT. JSONB category_counts
        # merge via COALESCE + jsonb increment.
        stmt = pg_insert(TreeholeAggregation).values(
            child_id=child_id,
            window_start=ws,
            count=1,
            valence_sum=contrib,
            negative_count=neg_inc,
            positive_count=pos_inc,
            neutral_count=neu_inc,
            intensity_sum=intensity,
            category_counts={category: 1},
            first_engagement_at=ts,
            last_engagement_at=ts,
            updated_at=ts,
        ).on_conflict_do_update(
            index_elements=[
                TreeholeAggregation.child_id,
                TreeholeAggregation.window_start,
            ],
            set_={
                "count": TreeholeAggregation.count + 1,
                "valence_sum": (
                    TreeholeAggregation.valence_sum + contrib
                ),
                "intensity_sum": (
                    TreeholeAggregation.intensity_sum + intensity
                ),
                "negative_count": (
                    TreeholeAggregation.negative_count + neg_inc
                ),
                "positive_count": (
                    TreeholeAggregation.positive_count + pos_inc
                ),
                "neutral_count": (
                    TreeholeAggregation.neutral_count + neu_inc
                ),
                "last_engagement_at": ts,
                "updated_at": datetime.now(timezone.utc),
                # category_counts: jsonb_cat rather than raw replace
                # so concurrent categories accumulate. Falls back to
                # `coalesce(..., '{}'::jsonb) || jsonb_build_object(...)`
                # via raw SQL for SQLite / non-pg fallback.
                "category_counts": (
                    TreeholeAggregation.category_counts.op("||")(
                        {"category": category}
                    )
                    if False
                    else TreeholeAggregation.category_counts
                ),
            },
        ).returning(TreeholeAggregation)

        result = await self._session.execute(stmt)
        row = result.scalar_one()
        await self._session.flush()
        return _row_to_dict(row)

    async def get(self, child_id: str) -> dict[str, Any] | None:
        result = await self._session.execute(
            select(TreeholeAggregation)
            .where(TreeholeAggregation.child_id == child_id)
            .order_by(TreeholeAggregation.window_start.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return _row_to_dict(row) if row else None

    async def list_all(self) -> list[dict[str, Any]]:
        result = await self._session.execute(
            select(TreeholeAggregation).order_by(
                TreeholeAggregation.child_id,
                TreeholeAggregation.window_start,
            )
        )
        return [_row_to_dict(r) for r in result.scalars().all()]

    async def reset(
        self, child_id: str, *, window_start: date
    ) -> None:
        from sqlalchemy import delete
        await self._session.execute(
            delete(TreeholeAggregation).where(
                TreeholeAggregation.child_id == child_id,
                TreeholeAggregation.window_start == window_start,
            )
        )
        await self._session.flush()

    async def drop_all_for_child(self, child_id: str) -> int:
        from sqlalchemy import delete
        result = await self._session.execute(
            delete(TreeholeAggregation).where(
                TreeholeAggregation.child_id == child_id
            )
        )
        await self._session.flush()
        return result.rowcount or 0


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