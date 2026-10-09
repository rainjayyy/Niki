"""LetterRepository abstraction for parent-to-child letters.

The contract: create, list for a child, and mark letters as read.
The default implementation is in-memory (single-process dev / tests);
production can swap in a Postgres-backed implementation by calling
`set_letter_repository(...)` at wiring time.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog

logger = structlog.get_logger(__name__)


class LetterRepository(ABC):
    """Contract for letter persistence."""

    @abstractmethod
    async def create(self, *, child_id: str, parent_id: str, content: str) -> dict[str, Any]:
        """Persist a new letter and return its serialized row."""

    @abstractmethod
    async def list_for_child(self, child_id: str) -> list[dict[str, Any]]:
        """List all letters sent to a child, oldest first."""

    @abstractmethod
    async def get(self, letter_id: str) -> dict[str, Any] | None:
        """Return a single letter row, or None."""

    @abstractmethod
    async def mark_read(self, letter_id: str) -> dict[str, Any] | None:
        """Mark a letter read and return the updated row."""

    @abstractmethod
    async def delete_all(self) -> int:
        """Drop all rows (debug / test only)."""


class InMemoryLetterRepository(LetterRepository):
    """In-process LetterRepository, safe for single-instance use."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    async def create(self, *, child_id: str, parent_id: str, content: str) -> dict[str, Any]:
        letter_id = str(uuid4())
        row: dict[str, Any] = {
            "id": letter_id,
            "child_id": child_id,
            "parent_id": parent_id,
            "from": "parent",
            "content": content,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "read": False,
        }
        with self._lock:
            self._rows[letter_id] = row
        logger.info(
            "letter_repo.created",
            letter_id=letter_id,
            child_id=child_id,
        )
        return dict(row)

    async def list_for_child(self, child_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(r) for r in self._rows.values() if r["child_id"] == child_id]
        return sorted(rows, key=lambda r: r["sent_at"])

    async def get(self, letter_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._rows.get(letter_id)
            return dict(row) if row else None

    async def mark_read(self, letter_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._rows.get(letter_id)
            if row is None:
                return None
            row["read"] = True
            return dict(row)

    async def delete_all(self) -> int:
        with self._lock:
            count = len(self._rows)
            self._rows.clear()
        return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


class PostgresLetterRepository(LetterRepository):
    """PostgreSQL-backed LetterRepository."""

    def __init__(self, session: Any) -> None:
        from sqlalchemy import delete, select

        from niki.db.models.letter import ParentLetter

        self._session = session
        self._select = select
        self._delete = delete
        self._model = ParentLetter

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "child_id": row.child_id,
            "parent_id": row.parent_id,
            "from": "parent",
            "content": row.content,
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            "read": bool(row.read),
        }

    async def create(self, *, child_id: str, parent_id: str, content: str) -> dict[str, Any]:
        row = self._model(
            id=str(uuid4()),
            child_id=child_id,
            parent_id=parent_id,
            content=content,
        )
        self._session.add(row)
        await self._session.flush()
        return self._to_dict(row)

    async def list_for_child(self, child_id: str) -> list[dict[str, Any]]:
        rows = (
            (
                await self._session.execute(
                    self._select(self._model)
                    .where(self._model.child_id == child_id)
                    .order_by(self._model.sent_at)
                )
            )
            .scalars()
            .all()
        )
        return [self._to_dict(r) for r in rows]

    async def get(self, letter_id: str) -> dict[str, Any] | None:
        row = (
            await self._session.execute(
                self._select(self._model).where(self._model.id == letter_id)
            )
        ).scalar_one_or_none()
        return self._to_dict(row) if row else None

    async def mark_read(self, letter_id: str) -> dict[str, Any] | None:
        row = (
            await self._session.execute(
                self._select(self._model).where(self._model.id == letter_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.read = True
        await self._session.flush()
        return self._to_dict(row)

    async def delete_all(self) -> int:
        result = await self._session.execute(self._delete(self._model))
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_repo: LetterRepository | None = None


def get_letter_repository() -> LetterRepository:
    """Return the process-wide letter repository (default: in-memory)."""
    global _repo
    if _repo is None:
        _repo = InMemoryLetterRepository()
    return _repo


def set_letter_repository(repo: LetterRepository) -> None:
    """Override the process-wide letter repository (tests / Postgres swap)."""
    global _repo
    _repo = repo
