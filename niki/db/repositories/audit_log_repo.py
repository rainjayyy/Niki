"""AuditLogRepository — persistence for the unified audit trail (BE-COMP-004).

Contract: append events, then read them back filtered by actor /
event_type / time window. Business code (`AuditLogService`, the
`/admin/audit` endpoint) depends only on the ABC.

Two implementations ship, matching the `auth_audit_repo` convention:
- `InMemoryAuditLogRepository` — default; tests + dev.
- `PostgresAuditLogRepository` — production; `audit_logs` table.

Returned rows are plain JSON-safe-ish dicts (datetimes stay as
datetimes; the API layer serialises) so callers stay decoupled from
SQLAlchemy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models.audit_log import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    VALID_SEVERITIES,
    AuditLog,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "AuditEvent",
    "AuditLogRepository",
    "InMemoryAuditLogRepository",
    "PostgresAuditLogRepository",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SEVERITY_CRITICAL",
    "get_audit_log_repository",
    "set_audit_log_repository",
]

_VALID_SEVERITIES = frozenset(VALID_SEVERITIES)


# ---------------------------------------------------------------------------
# Event dataclass + repository contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    """One cross-domain audit event ready to persist.

    `metadata` is a small free-form dict — counts, flags, versions.
    It MUST NOT carry PII (see the model docstring); `AuditLogService`
    is the layer that enforces that convention at the call sites.
    """

    event_type: str
    severity: str = SEVERITY_INFO
    actor_parent_id: Optional[str] = None
    target_parent_id: Optional[str] = None
    target_child_id: Optional[str] = None
    endpoint: Optional[str] = None
    method: Optional[str] = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    client_ip: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.event_type:
            raise ValueError("event_type must be a non-empty string")
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"invalid severity {self.severity!r}; "
                f"expected one of {sorted(_VALID_SEVERITIES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the event as a dict keyed by *ORM attribute* name."""
        return {
            "event_type": self.event_type,
            "severity": self.severity,
            "actor_parent_id": self.actor_parent_id,
            "target_parent_id": self.target_parent_id,
            "target_child_id": self.target_child_id,
            "endpoint": self.endpoint,
            "method": self.method,
            "reason": self.reason,
            "event_metadata": dict(self.metadata or {}),
            "client_ip": self.client_ip,
        }

    def to_row(self) -> dict[str, Any]:
        """Return the event as an API-shaped dict (`metadata` key)."""
        row = self.to_dict()
        row["metadata"] = row.pop("event_metadata")
        return row


class AuditLogRepository(ABC):
    """Contract for unified audit-log persistence."""

    @abstractmethod
    async def add(self, event: AuditEvent) -> str:
        """Persist an event; returns its assigned id."""

    @abstractmethod
    async def list_events(
        self,
        *,
        limit: int = 50,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """List events newest-first.

        Args:
            limit: Maximum rows to return.
            event_type: Exact-match filter on `event_type`.
            actor_parent_id: Exact-match filter on the acting parent.
            since: Keep rows with `created_at >= since` (inclusive).
        """

    @abstractmethod
    async def count(
        self,
        *,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
        since: datetime | None = None,
    ) -> int:
        """Count events matching the filters."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


def _as_aware(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC so comparisons never explode.

    SQLite round-trips `DateTime(timezone=True)` as naive, and callers
    may hand us either flavour of `since`. Normalising in one place
    keeps every comparison below total.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class InMemoryAuditLogRepository(AuditLogRepository):
    """Dict-backed in-memory AuditLogRepository."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._seq: int = 0

    async def add(self, event: AuditEvent) -> str:
        row_id = str(uuid4())
        self._seq += 1
        self._rows[row_id] = {
            "id": row_id,
            **event.to_row(),
            "created_at": datetime.now(timezone.utc),
            "_seq": self._seq,
        }
        return row_id

    async def list_events(
        self,
        *,
        limit: int = 50,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        # Sort by `_seq` (monotonic insertion counter) rather than
        # `created_at` so back-to-back adds inside the same
        # microsecond still come back in insertion order.
        rows = sorted(
            self._rows.values(),
            key=lambda r: r["_seq"],
            reverse=True,
        )
        rows = [r for r in rows if self._matches(
            r,
            event_type=event_type,
            actor_parent_id=actor_parent_id,
            since=since,
        )]
        # Strip the internal `_seq` so callers can't depend on it.
        return [
            {k: v for k, v in r.items() if k != "_seq"}
            for r in rows[:limit]
        ]

    async def count(
        self,
        *,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
        since: datetime | None = None,
    ) -> int:
        return sum(
            1
            for r in self._rows.values()
            if self._matches(
                r,
                event_type=event_type,
                actor_parent_id=actor_parent_id,
                since=since,
            )
        )

    @staticmethod
    def _matches(
        row: dict[str, Any],
        *,
        event_type: str | None,
        actor_parent_id: str | None,
        since: datetime | None,
    ) -> bool:
        if event_type is not None and row["event_type"] != event_type:
            return False
        if (
            actor_parent_id is not None
            and row.get("actor_parent_id") != actor_parent_id
        ):
            return False
        if since is not None:
            created = row.get("created_at")
            if created is None:
                return False
            if _as_aware(created) < _as_aware(since):
                return False
        return True

    def __len__(self) -> int:
        return len(self._rows)


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


def _row_to_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_type": row.event_type,
        "severity": row.severity,
        "actor_parent_id": row.actor_parent_id,
        "target_parent_id": row.target_parent_id,
        "target_child_id": row.target_child_id,
        "endpoint": row.endpoint,
        "method": row.method,
        "reason": row.reason,
        "metadata": dict(row.event_metadata or {}),
        "client_ip": row.client_ip,
        "created_at": row.created_at,
    }


class PostgresAuditLogRepository(AuditLogRepository):
    """AuditLogRepository backed by Postgres via SQLAlchemy async."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: AuditEvent) -> str:
        row = AuditLog(**event.to_dict())
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def list_events(
        self,
        *,
        limit: int = 50,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
        stmt = self._apply_filters(
            stmt,
            event_type=event_type,
            actor_parent_id=actor_parent_id,
            since=since,
        )
        result = await self._session.execute(stmt.limit(limit))
        return [_row_to_dict(r) for r in result.scalars().all()]

    async def count(
        self,
        *,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
        since: datetime | None = None,
    ) -> int:
        stmt = select(func.count(AuditLog.id))
        stmt = self._apply_filters(
            stmt,
            event_type=event_type,
            actor_parent_id=actor_parent_id,
            since=since,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar() or 0)

    @staticmethod
    def _apply_filters(
        stmt: Any,
        *,
        event_type: str | None,
        actor_parent_id: str | None,
        since: datetime | None,
    ) -> Any:
        if event_type is not None:
            stmt = stmt.where(AuditLog.event_type == event_type)
        if actor_parent_id is not None:
            stmt = stmt.where(AuditLog.actor_parent_id == actor_parent_id)
        if since is not None:
            stmt = stmt.where(AuditLog.created_at >= since)
        return stmt


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_repo: AuditLogRepository | None = None


def get_audit_log_repository() -> AuditLogRepository:
    """Return the process-wide audit-log repo (default: in-memory)."""
    global _repo
    if _repo is None:
        _repo = InMemoryAuditLogRepository()
    return _repo


def set_audit_log_repository(repo: AuditLogRepository) -> None:
    """Override the process-wide audit-log repo (tests / Postgres)."""
    global _repo
    _repo = repo
