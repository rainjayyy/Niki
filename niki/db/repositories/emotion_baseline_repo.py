"""EmotionBaselineRepository abstraction (BE-STORG-001-ext-D).

The contract: persist + retrieve EmotionRecord objects (7-day
rolling window) without the manager caring which backend.

Two implementations:
- `InMemoryEmotionBaselineRepository` — default; matches the legacy
  in-memory list behaviour.
- `PostgresEmotionBaselineRepository` — production; uses the
  `emotion_baseline_records` table.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models import EmotionBaselineRecord

logger = structlog.get_logger(__name__)


def _row_to_dict(row: EmotionBaselineRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "date": row.date,
        "valence": row.valence,
        "intensity": row.intensity,
        "source": row.source,
        "created_at": row.created_at,
    }


class EmotionBaselineRepository(ABC):
    """Contract for emotion baseline persistence."""

    @abstractmethod
    async def add(
        self,
        *,
        date: str,
        valence: float,
        intensity: int,
        source: str,
    ) -> int:
        """Persist a new record; returns its id."""

    @abstractmethod
    async def list_in_window(self, window_days: int = 30) -> list[dict[str, Any]]:
        """Return records from the last `window_days` days.

        Caller-side trimming is what the old in-memory behaviour did
        via `self._records = self._records[-30:]`. We push the same
        semantic into the repository so the manager doesn't need to
        know about it.
        """

    @abstractmethod
    async def list_all(self) -> list[dict[str, Any]]:
        """Return every record, no time filter."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryEmotionBaselineRepository(EmotionBaselineRepository):
    """In-memory EmotionBaselineRepository."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    async def add(
        self,
        *,
        date: str,
        valence: float,
        intensity: int,
        source: str,
    ) -> int:
        row_id = len(self._rows) + 1
        self._rows.append(
            {
                "id": row_id,
                "date": date,
                "valence": valence,
                "intensity": intensity,
                "source": source,
                "created_at": datetime.utcnow(),
            }
        )
        return row_id

    async def list_in_window(self, window_days: int = 30) -> list[dict[str, Any]]:
        cutoff = datetime.utcnow() - timedelta(days=window_days)
        return [r for r in self._rows if r["created_at"] >= cutoff]

    async def list_all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresEmotionBaselineRepository(EmotionBaselineRepository):
    """EmotionBaselineRepository backed by Postgres via SQLAlchemy async."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        date: str,
        valence: float,
        intensity: int,
        source: str,
    ) -> int:
        row = EmotionBaselineRecord(
            date=date,
            valence=valence,
            intensity=intensity,
            source=source,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def list_in_window(self, window_days: int = 30) -> list[dict[str, Any]]:
        cutoff = datetime.utcnow() - timedelta(days=window_days)
        result = await self._session.execute(
            select(EmotionBaselineRecord)
            .where(EmotionBaselineRecord.created_at >= cutoff)
            .order_by(EmotionBaselineRecord.created_at)
        )
        return [_row_to_dict(r) for r in result.scalars().all()]

    async def list_all(self) -> list[dict[str, Any]]:
        result = await self._session.execute(
            select(EmotionBaselineRecord).order_by(
                EmotionBaselineRecord.created_at
            )
        )
        return [_row_to_dict(r) for r in result.scalars().all()]


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_repo: EmotionBaselineRepository | None = None


def get_emotion_baseline_repository() -> EmotionBaselineRepository:
    """Return the process-wide emotion baseline repository."""
    global _repo
    if _repo is None:
        _repo = InMemoryEmotionBaselineRepository()
    return _repo


def set_emotion_baseline_repository(
    repo: EmotionBaselineRepository,
) -> None:
    """Override the process-wide emotion baseline repository."""
    global _repo
    _repo = repo
