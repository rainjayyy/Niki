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
from datetime import datetime, timezone
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
        "read_at": getattr(row, "read_at", None),
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

    # ---------------- BE-PUSH-005 notification center ----------------

    @abstractmethod
    async def list_for_parent(
        self,
        *,
        parent_id: str,
        severity_filter: list[str] | None = None,
        unread_only: bool = False,
        after_id: str | None = None,
        after_ts: datetime | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Cursor-paginated list for one parent (notification center).

        Sort: `created_at DESC, id DESC` (stable so a page boundary is
        unambiguous across same-timestamp rows).

        Filters (all optional, AND-combined):
          - `severity_filter`: case-insensitive match against
            `payload['severity']`. The caller is responsible for
            validating the values against the legal severity set.
          - `unread_only`: `read_at IS NULL`.
          - `after_id` / `after_ts`: cursor — only rows strictly older
            than the previous page's last row. The endpoint supplies
            `(after_id, after_ts)` from the decoded cursor. Both must
            be provided together; supplying only one is a programming
            error.

        Returns at most `limit` rows. If `severity_filter` is empty or
        None, no severity filter is applied.
        """

    @abstractmethod
    async def mark_alert_read(self, alert_id: str, parent_id: str) -> bool:
        """Mark an alert as read, scoped to `parent_id` (lateral-auth).

        Returns True iff a row owned by `parent_id` was updated. If the
        alert id exists but belongs to a different parent (or doesn't
        exist at all), returns False — caller surfaces a 404 so the
        endpoint doesn't leak whether the id exists.
        """

    @abstractmethod
    async def mark_all_read_for_parent(self, parent_id: str) -> int:
        """Mark every unread alert for `parent_id` as read.

        Returns the number of rows updated. Idempotent: calling on an
        already-caught-up parent returns 0.
        """

    @abstractmethod
    async def count_unread_for_parent(self, parent_id: str) -> int:
        """Return the number of unread alerts for `parent_id`."""

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
    async def list_tutoring_events(self, child_id: str) -> list[dict[str, Any]]:
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
    async def list_knowledge_queries(self, child_id: str) -> list[dict[str, Any]]:
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
            "created_at": datetime.now(timezone.utc),
            "delivered_at": None,
            "read_at": None,
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
        row["delivered_at"] = datetime.now(timezone.utc)
        return True

    # ---------------- BE-PUSH-005 notification center ----------------

    async def list_for_parent(
        self,
        *,
        parent_id: str,
        severity_filter: list[str] | None = None,
        unread_only: bool = False,
        after_id: str | None = None,
        after_ts: datetime | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        # Normalise severity filter to lower-case set for O(1) match.
        sev_set: set[str] | None = {s.lower() for s in severity_filter} if severity_filter else None
        # Step 1: filter by parent + severity + unread + cursor.
        matched: list[dict[str, Any]] = []
        for row in self._alerts.values():
            if row.get("parent_id") != parent_id:
                continue
            if sev_set is not None:
                payload = row.get("payload") or {}
                sev = str(payload.get("severity", "")).lower()
                if sev not in sev_set:
                    continue
            if unread_only and row.get("read_at") is not None:
                continue
            if after_ts is not None and after_id is not None:
                # Strict less-than on (created_at DESC, id DESC). A
                # row qualifies iff either (a) created_at < after_ts
                # OR (b) created_at == after_ts AND id < after_id.
                row_ts = row.get("created_at")
                row_id = row.get("id")
                if row_ts is None:
                    continue
                if row_ts > after_ts:
                    continue
                if row_ts == after_ts and row_id >= after_id:
                    continue
            matched.append(row)
        # Step 2: sort — newest first, stable on id as tie-breaker.
        matched.sort(
            key=lambda r: (r.get("created_at"), r.get("id")),
            reverse=True,
        )
        # Step 3: limit.
        return [dict(r) for r in matched[:limit]]

    async def mark_alert_read(self, alert_id: str, parent_id: str) -> bool:
        row = self._alerts.get(alert_id)
        if row is None or row.get("parent_id") != parent_id:
            return False
        row["read_at"] = datetime.now(timezone.utc)
        return True

    async def mark_all_read_for_parent(self, parent_id: str) -> int:
        now = datetime.now(timezone.utc)
        marked = 0
        for row in self._alerts.values():
            if row.get("parent_id") == parent_id and row.get("read_at") is None:
                row["read_at"] = now
                marked += 1
        return marked

    async def count_unread_for_parent(self, parent_id: str) -> int:
        return sum(
            1
            for r in self._alerts.values()
            if r.get("parent_id") == parent_id and r.get("read_at") is None
        )

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
            "created_at": datetime.now(timezone.utc),
        }
        return row_id

    async def list_tutoring_events(self, child_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._tutoring.values() if r["child_id"] == child_id]

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
            "created_at": datetime.now(timezone.utc),
        }
        return row_id

    async def list_knowledge_queries(self, child_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._knowledge.values() if r["child_id"] == child_id]

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
            .values(delivered_at=datetime.now(timezone.utc))
        )
        return (result.rowcount or 0) > 0

    # ---------------- BE-PUSH-005 notification center ----------------

    async def list_for_parent(
        self,
        *,
        parent_id: str,
        severity_filter: list[str] | None = None,
        unread_only: bool = False,
        after_id: str | None = None,
        after_ts: datetime | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        # Severity is stored in `payload['severity']`. We use JSON text
        # extraction via `->>` so the comparison is portable across
        # SQLite (tests) and Postgres JSONB (prod). Lower-case match on
        # the Python side keeps the SQL portable; if the volume grows
        # we can switch to a generated `severity_text` column with an
        # index, but at the current scale (≤ 1000/parent) a per-row
        # filter is fine.
        from sqlalchemy import func, or_, tuple_

        stmt = (
            select(DashboardAlert)
            .where(DashboardAlert.parent_id == parent_id)
            .order_by(
                DashboardAlert.created_at.desc(),
                DashboardAlert.id.desc(),
            )
            .limit(limit)
        )
        if unread_only:
            stmt = stmt.where(DashboardAlert.read_at.is_(None))
        if after_ts is not None and after_id is not None:
            # Strict less-than on (created_at, id) DESC: a row qualifies
            # iff (created_at < after_ts) OR (created_at == after_ts AND
            # id < after_id). The composite form keeps the planner happy.
            stmt = stmt.where(
                or_(
                    DashboardAlert.created_at < after_ts,
                    tuple_(
                        DashboardAlert.created_at,
                        DashboardAlert.id,
                    )
                    < tuple_(after_ts, after_id),
                )
            )
        result = await self._session.execute(stmt)
        rows = [_row_to_dict_alert(r) for r in result.scalars().all()]
        if severity_filter:
            sev_set = {s.lower() for s in severity_filter}
            rows = [
                r
                for r in rows
                if str((r.get("payload") or {}).get("severity", "")).lower() in sev_set
            ]
        return rows

    async def mark_alert_read(self, alert_id: str, parent_id: str) -> bool:
        result = await self._session.execute(
            update(DashboardAlert)
            .where(DashboardAlert.id == alert_id)
            .where(DashboardAlert.parent_id == parent_id)
            .values(read_at=datetime.now(timezone.utc))
        )
        return (result.rowcount or 0) > 0

    async def mark_all_read_for_parent(self, parent_id: str) -> int:
        from sqlalchemy import update as sa_update

        result = await self._session.execute(
            sa_update(DashboardAlert)
            .where(DashboardAlert.parent_id == parent_id)
            .where(DashboardAlert.read_at.is_(None))
            .values(read_at=datetime.now(timezone.utc))
        )
        return result.rowcount or 0

    async def count_unread_for_parent(self, parent_id: str) -> int:
        from sqlalchemy import func as sa_func

        result = await self._session.execute(
            select(sa_func.count(DashboardAlert.id))
            .where(DashboardAlert.parent_id == parent_id)
            .where(DashboardAlert.read_at.is_(None))
        )
        return int(result.scalar() or 0)

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

    async def list_tutoring_events(self, child_id: str) -> list[dict[str, Any]]:
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

    async def list_knowledge_queries(self, child_id: str) -> list[dict[str, Any]]:
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
