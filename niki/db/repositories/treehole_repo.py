"""TreeholeRepository abstraction (BE-STORG-001-ext-B).

The contract: persist, list, and purge treehole encrypted payloads.

Two implementations:
- `InMemoryTreeholeRepository` — default; matches the legacy dict-of-
  dicts storage behaviour for tests / single-process dev.
- `PostgresTreeholeRepository` — production; uses the
  `treehole_engagements` table (BE-STORG-001-ext-B).

Returned rows are plain dicts (not ORM objects) so the
`TreeholeManager` doesn't depend on SQLAlchemy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models import TreeholeEngagement

logger = structlog.get_logger(__name__)


def _row_to_dict(row: TreeholeEngagement) -> dict[str, Any]:
    """Map an ORM row to the dict shape callers expect."""
    return {
        "id": row.id,
        "child_id": row.child_id,
        "parent_id": row.parent_id,
        "encrypted_payload": bytes(row.encrypted_payload),
        "associated_data": bytes(row.associated_data) if row.associated_data else None,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
    }


class TreeholeRepository(ABC):
    """Contract for treehole payload persistence."""

    @abstractmethod
    async def add(
        self,
        *,
        child_id: str,
        parent_id: str,
        encrypted_payload: bytes,
        associated_data: bytes | None,
        expires_at: datetime,
    ) -> str:
        """Persist a new engagement; returns the assigned id."""

    @abstractmethod
    async def list_for_child(self, child_id: str) -> list[dict[str, Any]]:
        """List all non-expired engagements for a child."""

    @abstractmethod
    async def list_all(self) -> list[dict[str, Any]]:
        """List every (non-expired) engagement across all children."""

    @abstractmethod
    async def delete_expired(self, now: datetime | None = None) -> int:
        """Purge rows whose `expires_at` is in the past; returns count."""

    @abstractmethod
    async def delete_all_for_child(self, child_id: str) -> int:
        """Drop every engagement for a single child (PRD #35)."""

    @abstractmethod
    async def delete_all(self) -> int:
        """Drop every engagement (debug / test only)."""


class InMemoryTreeholeRepository(TreeholeRepository):
    """In-process TreeholeRepository — fast, single-instance only."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    async def add(
        self,
        *,
        child_id: str,
        parent_id: str,
        encrypted_payload: bytes,
        associated_data: bytes | None,
        expires_at: datetime,
    ) -> str:
        row_id = str(uuid4())
        self._rows[row_id] = {
            "id": row_id,
            "child_id": child_id,
            "parent_id": parent_id,
            "encrypted_payload": encrypted_payload,
            "associated_data": associated_data,
            "expires_at": expires_at,
            "created_at": datetime.utcnow(),
        }
        return row_id

    async def list_for_child(self, child_id: str) -> list[dict[str, Any]]:
        now = datetime.utcnow()
        return [
            dict(r)
            for r in self._rows.values()
            if r["child_id"] == child_id and r["expires_at"] > now
        ]

    async def list_all(self) -> list[dict[str, Any]]:
        now = datetime.utcnow()
        return [
            dict(r) for r in self._rows.values() if r["expires_at"] > now
        ]

    async def delete_expired(self, now: datetime | None = None) -> int:
        now = now or datetime.utcnow()
        expired = [
            k for k, r in self._rows.items() if r["expires_at"] <= now
        ]
        for k in expired:
            self._rows.pop(k, None)
        if expired:
            logger.info(
                "treehole_repo.purged_expired", count=len(expired)
            )
        return len(expired)

    async def delete_all_for_child(self, child_id: str) -> int:
        keys = [
            k
            for k, r in self._rows.items()
            if r["child_id"] == child_id
        ]
        for k in keys:
            self._rows.pop(k, None)
        if keys:
            logger.info(
                "treehole_repo.purged_for_child", child_id=child_id, count=len(keys)
            )
        return len(keys)

    async def delete_all(self) -> int:
        count = len(self._rows)
        self._rows.clear()
        logger.warning("treehole_repo.purged_all", count=count)
        return count

    def __len__(self) -> int:
        return len(self._rows)


class PostgresTreeholeRepository(TreeholeRepository):
    """TreeholeRepository backed by Postgres via SQLAlchemy async."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        child_id: str,
        parent_id: str,
        encrypted_payload: bytes,
        associated_data: bytes | None,
        expires_at: datetime,
    ) -> str:
        row = TreeholeEngagement(
            child_id=child_id,
            parent_id=parent_id,
            encrypted_payload=encrypted_payload,
            associated_data=associated_data,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def list_for_child(self, child_id: str) -> list[dict[str, Any]]:
        now = datetime.utcnow()
        result = await self._session.execute(
            select(TreeholeEngagement)
            .where(
                TreeholeEngagement.child_id == child_id,
                TreeholeEngagement.expires_at > now,
            )
            .order_by(TreeholeEngagement.created_at)
        )
        return [_row_to_dict(r) for r in result.scalars().all()]

    async def list_all(self) -> list[dict[str, Any]]:
        now = datetime.utcnow()
        result = await self._session.execute(
            select(TreeholeEngagement)
            .where(TreeholeEngagement.expires_at > now)
            .order_by(TreeholeEngagement.created_at)
        )
        return [_row_to_dict(r) for r in result.scalars().all()]

    async def delete_expired(self, now: datetime | None = None) -> int:
        now = now or datetime.utcnow()
        result = await self._session.execute(
            delete(TreeholeEngagement).where(
                TreeholeEngagement.expires_at <= now
            )
        )
        count = result.rowcount or 0
        if count:
            logger.info("treehole_repo.purged_expired", count=count)
        return count

    async def delete_all_for_child(self, child_id: str) -> int:
        result = await self._session.execute(
            delete(TreeholeEngagement).where(
                TreeholeEngagement.child_id == child_id
            )
        )
        count = result.rowcount or 0
        if count:
            logger.info(
                "treehole_repo.purged_for_child", child_id=child_id, count=count
            )
        return count

    async def delete_all(self) -> int:
        result = await self._session.execute(delete(TreeholeEngagement))
        count = result.rowcount or 0
        logger.warning("treehole_repo.purged_all", count=count)
        return count


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_repo: TreeholeRepository | None = None


def get_treehole_repository() -> TreeholeRepository:
    """Return the process-wide treehole repository (default: in-memory)."""
    global _repo
    if _repo is None:
        _repo = InMemoryTreeholeRepository()
    return _repo


def set_treehole_repository(repo: TreeholeRepository) -> None:
    """Override the process-wide treehole repository (for tests / Postgres swap)."""
    global _repo
    _repo = repo