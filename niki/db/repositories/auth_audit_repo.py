"""AuthAuditLogRepository abstraction (BE-AUTH-001-rollout-audit).

Contract: persist + retrieve auth audit events for the security
trail. Business code (the lateral-auth dep, future SIEM exporters)
depends only on the ABC.

Two implementations ship:
- `InMemoryAuthAuditLogRepository` — default; tests + dev.
- `PostgresAuthAuditLogRepository` — production; uses the
  `auth_audit_logs` table (this ticket's new schema).

Returned rows are plain dicts so callers stay decoupled from
SQLAlchemy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models import AuthAuditLog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Event dataclass + repository contract
# ---------------------------------------------------------------------------


SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

_VALID_SEVERITIES = frozenset({SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL})


@dataclass(frozen=True)
class AuthAuditEvent:
    """One auth-related event ready to persist.

    The factory helpers (`violation`, `info`) apply the standard
    severity conventions so call sites don't pass strings.
    """

    event_type: str
    severity: str = SEVERITY_INFO
    actor_parent_id: Optional[str] = None
    target_parent_id: Optional[str] = None
    target_child_id: Optional[str] = None
    endpoint: str = ""
    method: str = ""
    reason: str = ""
    client_ip: Optional[str] = None

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"invalid severity {self.severity!r}; expected one of {sorted(_VALID_SEVERITIES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "severity": self.severity,
            "actor_parent_id": self.actor_parent_id,
            "target_parent_id": self.target_parent_id,
            "target_child_id": self.target_child_id,
            "endpoint": self.endpoint,
            "method": self.method,
            "reason": self.reason,
            "client_ip": self.client_ip,
        }

    @classmethod
    def violation(
        cls,
        event_type: str,
        *,
        actor_parent_id: str | None,
        reason: str,
        endpoint: str,
        method: str,
        target_parent_id: str | None = None,
        target_child_id: str | None = None,
        client_ip: str | None = None,
    ) -> "AuthAuditEvent":
        """Build a `warning`-severity violation event."""
        return cls(
            event_type=event_type,
            severity=SEVERITY_WARNING,
            actor_parent_id=actor_parent_id,
            target_parent_id=target_parent_id,
            target_child_id=target_child_id,
            endpoint=endpoint,
            method=method,
            reason=reason,
            client_ip=client_ip,
        )


class AuthAuditLogRepository(ABC):
    """Contract for auth audit persistence."""

    @abstractmethod
    async def add(self, event: AuthAuditEvent) -> str:
        """Persist an event; returns its assigned id."""

    @abstractmethod
    async def list_recent(
        self,
        *,
        limit: int = 100,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List events newest-first.

        Args:
            limit: Maximum rows (capped at 200 by the API layer).
            event_type: Filter to a single event_type if provided.
            actor_parent_id: Filter to a single actor if provided.
        """

    @abstractmethod
    async def count(
        self,
        *,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> int:
        """Count events matching the filters."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryAuthAuditLogRepository(AuthAuditLogRepository):
    """Dict-backed in-memory AuthAuditLogRepository."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._seq: int = 0

    async def add(self, event: AuthAuditEvent) -> str:
        row_id = str(uuid4())
        self._seq += 1
        self._rows[row_id] = {
            "id": row_id,
            **event.to_dict(),
            "created_at": datetime.now(timezone.utc),
            "_seq": self._seq,
        }
        return row_id

    async def list_recent(
        self,
        *,
        limit: int = 100,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # Sort by `_seq` (monotonic insertion counter) instead of
        # `created_at` so back-to-back adds inside the same microsecond
        # still come back in insertion order.
        rows = sorted(
            self._rows.values(),
            key=lambda r: r["_seq"],
            reverse=True,
        )
        if event_type is not None:
            rows = [r for r in rows if r["event_type"] == event_type]
        if actor_parent_id is not None:
            rows = [r for r in rows if r.get("actor_parent_id") == actor_parent_id]
        # Strip internal _seq from the returned rows so callers
        # don't depend on an implementation detail.
        out = []
        for r in rows[:limit]:
            item = {k: v for k, v in r.items() if k != "_seq"}
            out.append(item)
        return out

    async def count(
        self,
        *,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> int:
        n = 0
        for row in self._rows.values():
            if event_type is not None and row["event_type"] != event_type:
                continue
            if since is not None and row["created_at"] < since:
                continue
            n += 1
        return n

    def __len__(self) -> int:
        return len(self._rows)


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


def _row_to_dict(row: AuthAuditLog) -> dict[str, Any]:
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
        "client_ip": row.client_ip,
        "created_at": row.created_at,
    }


class PostgresAuthAuditLogRepository(AuthAuditLogRepository):
    """AuthAuditLogRepository backed by Postgres via SQLAlchemy async."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: AuthAuditEvent) -> str:
        row = AuthAuditLog(**event.to_dict())
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def list_recent(
        self,
        *,
        limit: int = 100,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(AuthAuditLog).order_by(AuthAuditLog.created_at.desc())
        if event_type is not None:
            stmt = stmt.where(AuthAuditLog.event_type == event_type)
        if actor_parent_id is not None:
            stmt = stmt.where(AuthAuditLog.actor_parent_id == actor_parent_id)
        stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [_row_to_dict(r) for r in result.scalars().all()]

    async def count(
        self,
        *,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> int:
        stmt = select(func.count(AuthAuditLog.id))
        if event_type is not None:
            stmt = stmt.where(AuthAuditLog.event_type == event_type)
        if since is not None:
            stmt = stmt.where(AuthAuditLog.created_at >= since)
        result = await self._session.execute(stmt)
        return int(result.scalar() or 0)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_repo: AuthAuditLogRepository | None = None


def get_auth_audit_log_repository() -> AuthAuditLogRepository:
    """Return the process-wide auth-audit repo (default: in-memory)."""
    global _repo
    if _repo is None:
        _repo = InMemoryAuthAuditLogRepository()
    return _repo


def set_auth_audit_log_repository(repo: AuthAuditLogRepository) -> None:
    """Override the process-wide auth-audit repo (tests / Postgres)."""
    global _repo
    _repo = repo
