"""DashboardRepository abstraction (BE-STORG-001-ext-C).

The contract: persist + retrieve dashboard data (alerts, events,
queries) without the manager caring which backend it talks to.

Two implementations:
- `InMemoryDashboardRepository` — default; matches the legacy
  dict-of-lists behaviour for tests / single-process dev.
- `PostgresDashboardRepository` — production; uses SQLAlchemy
  async session against the dashboard_* tables.

Returned rows are plain dicts (not ORM objects) so the
`DashboardService` stays decoupled from SQLAlchemy.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models import (
    DashboardAlert,
    KnowledgeQuery,
    TutoringEvent,
)

logger = structlog.get_logger(__name__)


def _row_to_dict_alert(row: DashboardAlert) -> dict[str, Any]:
    return {
        "id": row.id,
        "alert_type": row.alert_type,
        "child_id": row.child_id,
        "parent_id": row.parent_id,
        "payload": dict(row.payload or {}),
        "created_at": row.created_at,
        "delivered_at": row.delivered_at,
    }


def _row_to_dict_event(row: TutoringEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "child_id": row.child_id,
        "parent_id": row.parent_id,
        "payload": dict(row.payload or {}),
        "created_at": row.created_at,
    }


def _row_to_dict_query(row: KnowledgeQuery) -> dict[str, Any]:
    return {
        "id": row.id,
        "child_id": row.child_id,
        "parent_id": row.parent_id,
        "payload": dict(row.payload or {}),
        "created_at": row.created_at,
    }


class DashboardRepository(ABC):
    """Contract for dashboard persistence."""

    # ---------------- Alerts ----------------

    @abstractmethod
    async def add_alert(
        self,
        *,
        alert_type: str,
        child_id: str,
        parent_id: str,
        payload: dict[str, Any],
    ) -> str:
        """Persist a new alert; returns its id."""

    @abstractmethod
    async def list_alerts(
        self,
        *,
        alert_type: str | None = None,
        child_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List alerts; both filters are optional (AND-combined)."""

    @abstractmethod
    async def mark_alert_delivered(self, alert_id: str) -> bool:
        """Mark an alert as delivered; returns True if a row was updated."""

    # ---------------- Tutoring events ----------------

    @abstractmethod
    async def add_tutoring_event(
        self,
        *,
        child_id: str,
        parent_id: str,
        payload: dict[str, Any],
    ) -> str:
        """Persist a tutoring event; returns its id."""

    @abstractmethod
    async def list_tutoring_events(
        self, child_id: str
    ) -> list[dict[str, Any]]:
        """List all tutoring events for a child."""

    # ---------------- Knowledge queries ----------------

    @abstractmethod
    async def add_knowledge_query(
        self,
        *,
        child_id: str,
        parent_id: str,
        payload: dict[str, Any],
    ) -> str:
        """Persist a knowledge query; returns its id."""

    @abstractmethod
    async def list_knowledge_queries(
        self, child_id: str
    ) -> list[dict[str, Any]]:
        """List all knowledge queries for a child."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryDashboardRepository(DashboardRepository):
    """Dict-backed in-memory DashboardRepository."""

    def __init__(self) -> None:
        self._alerts: dict[str, dict[str, Any]] = {}
        self._tutoring: dict[str, dict[str, Any]] = {}
        self._knowledge: dict[str, dict[str, Any]] = {}

    async def add_alert(
        self,
        *,
        alert_type: str,
        child_id: str,
        parent_id: str,
        payload: dict[str, Any],
    ) -> str:
        row_id = str(uuid.uuid4())
        self._alerts[row_id] = {
            "id": row_id,
            "alert_type": alert_type,
            "child_id": child_id,
            "parent_id": parent_id,
            "payload": dict(payload),
            "created_at": datetime.utcnow(),
            "delivered_at": None,
        }
        return row_id

    async def list_alerts(
        self,
        *,
        alert_type: str | None = None,
        child_id: str | None = None,
    ) -> list[dict[str, Any]]:
        out = []
        for row in self._alerts.values():
            if alert_type is not None and row["alert_type"] != alert_type:
                continue
            if child_id is not None and row["child_id"] != child_id:
                continue
            out.append(dict(row))
        return out

    async def mark_alert_delivered(self, alert_id: str) -> bool:
        row = self._alerts.get(alert_id)
        if row is None:
            return False
        row["delivered_at"] = datetime.utcnow()
        return True

    async def add_tutoring_event(
        self,
        *,
        child_id: str,
        parent_id: str,
        payload: dict[str, Any],
    ) -> str:
        row_id = str(uuid.uuid4())
        self._tutoring[row_id] = {
            "id": row_id,
            "child_id": child_id,
            "parent_id": parent_id,
            "payload": dict(payload),
            "created_at": datetime.utcnow(),
        }
        return row_id

    async def list_tutoring_events(
        self, child_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._tutoring.values()
            if r["child_id"] == child_id
        ]

    async def add_knowledge_query(
        self,
        *,
        child_id: str,
        parent_id: str,
        payload: dict[str, Any],
    ) -> str:
        row_id = str(uuid.uuid4())
        self._knowledge[row_id] = {
            "id": row_id,
            "child_id": child_id,
            "parent_id": parent_id,
            "payload": dict(payload),
            "created_at": datetime.utcnow(),
        }
        return row_id

    async def list_knowledge_queries(
        self, child_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._knowledge.values()
            if r["child_id"] == child_id
        ]

    def __len__(self) -> int:
        return len(self._alerts) + len(self._tutoring) + len(self._knowledge)


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresDashboardRepository(DashboardRepository):
    """DashboardRepository backed by Postgres via SQLAlchemy async."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_alert(
        self,
        *,
        alert_type: str,
        child_id: str,
        parent_id: str,
        payload: dict[str, Any],
    ) -> str:
        row = DashboardAlert(
            alert_type=alert_type,
            child_id=child_id,
            parent_id=parent_id,
            payload=dict(payload),
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def list_alerts(
        self,
        *,
        alert_type: str | None = None,
        child_id: str | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(DashboardAlert).order_by(DashboardAlert.created_at)
        if alert_type is not None:
            stmt = stmt.where(DashboardAlert.alert_type == alert_type)
        if child_id is not None:
            stmt = stmt.where(DashboardAlert.child_id == child_id)
        result = await self._session.execute(stmt)
        return [_row_to_dict_alert(r) for r in result.scalars().all()]

    async def mark_alert_delivered(self, alert_id: str) -> bool:
        result = await self._session.execute(
            update(DashboardAlert)
            .where(DashboardAlert.id == alert_id)
            .values(delivered_at=datetime.utcnow())
        )
        return (result.rowcount or 0) > 0

    async def add_tutoring_event(
        self,
        *,
        child_id: str,
        parent_id: str,
        payload: dict[str, Any],
    ) -> str:
        row = TutoringEvent(
            child_id=child_id,
            parent_id=parent_id,
            payload=dict(payload),
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def list_tutoring_events(
        self, child_id: str
    ) -> list[dict[str, Any]]:
        result = await self._session.execute(
            select(TutoringEvent)
            .where(TutoringEvent.child_id == child_id)
            .order_by(TutoringEvent.created_at)
        )
        return [_row_to_dict_event(r) for r in result.scalars().all()]

    async def add_knowledge_query(
        self,
        *,
        child_id: str,
        parent_id: str,
        payload: dict[str, Any],
    ) -> str:
        row = KnowledgeQuery(
            child_id=child_id,
            parent_id=parent_id,
            payload=dict(payload),
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def list_knowledge_queries(
        self, child_id: str
    ) -> list[dict[str, Any]]:
        result = await self._session.execute(
            select(KnowledgeQuery)
            .where(KnowledgeQuery.child_id == child_id)
            .order_by(KnowledgeQuery.created_at)
        )
        return [_row_to_dict_query(r) for r in result.scalars().all()]


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_repo: DashboardRepository | None = None


def get_dashboard_repository() -> DashboardRepository:
    """Return the process-wide dashboard repository (in-memory default)."""
    global _repo
    if _repo is None:
        _repo = InMemoryDashboardRepository()
    return _repo


def set_dashboard_repository(repo: DashboardRepository) -> None:
    """Override the process-wide dashboard repository (for tests / Postgres)."""
    global _repo
    _repo = repo
