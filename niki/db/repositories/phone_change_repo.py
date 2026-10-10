"""PhoneChangeRepository — persistence facade for parent phone-change
requests (BE-AUTH-006).

Two implementations:

* `PostgresPhoneChangeRepository`  — production; SQLAlchemy async +
  `phone_change_requests` table.
* `InMemoryPhoneChangeRepository`  — tests + dev fallback; dict + list.

Both expose the same ABC. Reads return the ORM row (Postgres) or
a lightweight dataclass (`PhoneChangeRequestData`); the service
layer never has to care which it has.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models.phone_change import (
    PhoneChangeRequest,
    PhoneChangeStatus,
    VALID_PHONE_CHANGE_STATUSES,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "PhoneChangeRequestData",
    "PhoneChangeRepository",
    "InMemoryPhoneChangeRepository",
    "PostgresPhoneChangeRepository",
    "get_phone_change_repository",
    "set_phone_change_repository",
]


@dataclass
class PhoneChangeRequestData:
    """Plain-data view of a PhoneChangeRequest row.

    Returned by both impls so the service layer works against a
    single shape regardless of backend.
    """

    id: str
    parent_id: str
    old_phone_hash: str
    new_phone_hash: str
    verification_code: str
    requested_at: datetime
    expires_at: datetime
    effective_at: Optional[datetime] = None
    status: str = PhoneChangeStatus.PENDING_VERIFICATION.value
    confirmed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    # In-memory backend only: stash the raw new phone so the flip
    # in `_complete()` can hand it to `account_repo.update_phone_hash`.
    # Production Postgres cannot recover a phone from its hash, so
    # this stays None there and `update_phone_hash` is a no-op
    # outside the dev / test flow.
    new_phone_raw: Optional[str] = None

    def is_pending(self) -> bool:
        return self.status in (
            PhoneChangeStatus.PENDING_VERIFICATION.value,
            PhoneChangeStatus.PENDING_COOLING.value,
        )

    def to_public_dict(self) -> dict:
        """Projection safe to return through the API. Excludes the
        verification_code (it's a secret)."""
        return {
            "request_id": self.id,
            "parent_id": self.parent_id,
            "status": self.status,
            "requested_at": self.requested_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "effective_at": (self.effective_at.isoformat() if self.effective_at else None),
            "confirmed_at": (self.confirmed_at.isoformat() if self.confirmed_at else None),
            "completed_at": (self.completed_at.isoformat() if self.completed_at else None),
            "cancelled_at": (self.cancelled_at.isoformat() if self.cancelled_at else None),
        }


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class PhoneChangeRepository(ABC):
    """Persistence facade for parent phone-change requests."""

    @abstractmethod
    async def create(
        self,
        *,
        parent_id: str,
        old_phone_hash: str,
        new_phone_hash: str,
        verification_code: str,
        expires_at: datetime,
        new_phone_raw: Optional[str] = None,
    ) -> PhoneChangeRequestData:
        """Insert a new PENDING_VERIFICATION row, return persisted.

        `new_phone_raw` is only populated by the in-memory backend —
        production cannot recover a phone from its hash, so the
        Postgres impl persists the column as NULL and the
        completion path is a no-op there (cooling sweep ends in a
        status flip without the hash swap)."""

    @abstractmethod
    async def get(self, request_id: str) -> PhoneChangeRequestData | None:
        """Look up by id, or None."""

    @abstractmethod
    async def list_inflight(self, parent_id: str) -> list[PhoneChangeRequestData]:
        """Return this parent's non-finalised rows
        (pending_verification + pending_cooling)."""

    @abstractmethod
    async def list_due_for_completion(self, *, now: datetime) -> list[PhoneChangeRequestData]:
        """Return `pending_cooling` rows whose `effective_at <= now`.

        Used by `PhoneChangeService.sweep_expired` (cron path).
        """

    @abstractmethod
    async def update_state(
        self,
        request_id: str,
        *,
        status: str,
        effective_at: Optional[datetime] = None,
        confirmed_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        cancelled_at: Optional[datetime] = None,
    ) -> PhoneChangeRequestData | None:
        """Mutate the state-machine columns. Returns the new state."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryPhoneChangeRepository(PhoneChangeRepository):
    """Dict-backed in-memory phone-change repo (tests + dev)."""

    def __init__(self) -> None:
        self._rows: dict[str, PhoneChangeRequestData] = {}

    async def create(
        self,
        *,
        parent_id: str,
        old_phone_hash: str,
        new_phone_hash: str,
        verification_code: str,
        expires_at: datetime,
        new_phone_raw: Optional[str] = None,
    ) -> PhoneChangeRequestData:
        import uuid as _uuid

        rid = str(_uuid.uuid4())
        now = datetime.now(timezone.utc)
        row = PhoneChangeRequestData(
            id=rid,
            parent_id=parent_id,
            old_phone_hash=old_phone_hash,
            new_phone_hash=new_phone_hash,
            verification_code=verification_code,
            requested_at=now,
            expires_at=cast("datetime", _aware(expires_at)),
            status=PhoneChangeStatus.PENDING_VERIFICATION.value,
            new_phone_raw=new_phone_raw,
        )
        self._rows[rid] = row
        return copy.deepcopy(row)

    async def get(self, request_id: str) -> PhoneChangeRequestData | None:
        row = self._rows.get(request_id)
        return copy.deepcopy(row) if row else None

    async def list_inflight(self, parent_id: str) -> list[PhoneChangeRequestData]:
        result: list[PhoneChangeRequestData] = []
        for row in self._rows.values():
            if row.parent_id == parent_id and row.status in (
                PhoneChangeStatus.PENDING_VERIFICATION.value,
                PhoneChangeStatus.PENDING_COOLING.value,
            ):
                result.append(copy.deepcopy(row))
        return result

    async def list_due_for_completion(self, *, now: datetime) -> list[PhoneChangeRequestData]:
        result: list[PhoneChangeRequestData] = []
        for row in self._rows.values():
            if (
                row.status == PhoneChangeStatus.PENDING_COOLING.value
                and row.effective_at is not None
                and row.effective_at <= now
            ):
                result.append(copy.deepcopy(row))
        return result

    async def update_state(
        self,
        request_id: str,
        *,
        status: str,
        effective_at: Optional[datetime] = None,
        confirmed_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        cancelled_at: Optional[datetime] = None,
    ) -> PhoneChangeRequestData | None:
        if status not in VALID_PHONE_CHANGE_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        row = self._rows.get(request_id)
        if row is None:
            return None
        row.status = status
        if effective_at is not None:
            row.effective_at = _aware(effective_at)
        if confirmed_at is not None:
            row.confirmed_at = _aware(confirmed_at)
        if completed_at is not None:
            row.completed_at = _aware(completed_at)
        if cancelled_at is not None:
            row.cancelled_at = _aware(cancelled_at)
        return copy.deepcopy(row)


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresPhoneChangeRepository(PhoneChangeRepository):
    """Phone-change repo backed by Postgres via SQLAlchemy async."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        parent_id: str,
        old_phone_hash: str,
        new_phone_hash: str,
        verification_code: str,
        expires_at: datetime,
        new_phone_raw: Optional[str] = None,
    ) -> PhoneChangeRequestData:
        row = PhoneChangeRequest(
            parent_id=parent_id,
            old_phone_hash=old_phone_hash,
            new_phone_hash=new_phone_hash,
            verification_code=verification_code,
            expires_at=_aware(expires_at),
        )
        self._session.add(row)
        await self._session.flush()
        return self._to_data(row)

    async def get(self, request_id: str) -> PhoneChangeRequestData | None:
        result = await self._session.execute(
            select(PhoneChangeRequest).where(PhoneChangeRequest.id == request_id)
        )
        row = result.scalar_one_or_none()
        return self._to_data(row) if row else None

    async def list_inflight(self, parent_id: str) -> list[PhoneChangeRequestData]:
        result = await self._session.execute(
            select(PhoneChangeRequest).where(
                PhoneChangeRequest.parent_id == parent_id,
                PhoneChangeRequest.status.in_(
                    [
                        PhoneChangeStatus.PENDING_VERIFICATION.value,
                        PhoneChangeStatus.PENDING_COOLING.value,
                    ]
                ),
            )
        )
        rows = result.scalars().all()
        return [self._to_data(r) for r in rows]

    async def list_due_for_completion(self, *, now: datetime) -> list[PhoneChangeRequestData]:
        result = await self._session.execute(
            select(PhoneChangeRequest).where(
                PhoneChangeRequest.status == PhoneChangeStatus.PENDING_COOLING.value,
                PhoneChangeRequest.effective_at <= now,
            )
        )
        rows = result.scalars().all()
        return [self._to_data(r) for r in rows]

    async def update_state(
        self,
        request_id: str,
        *,
        status: str,
        effective_at: Optional[datetime] = None,
        confirmed_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        cancelled_at: Optional[datetime] = None,
    ) -> PhoneChangeRequestData | None:
        if status not in VALID_PHONE_CHANGE_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        result = await self._session.execute(
            select(PhoneChangeRequest).where(PhoneChangeRequest.id == request_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.status = status
        if effective_at is not None:
            row.effective_at = _aware(effective_at)
        if confirmed_at is not None:
            row.confirmed_at = _aware(confirmed_at)
        if completed_at is not None:
            row.completed_at = _aware(completed_at)
        if cancelled_at is not None:
            row.cancelled_at = _aware(cancelled_at)
        await self._session.flush()
        return self._to_data(row)

    @staticmethod
    def _to_data(row: PhoneChangeRequest) -> PhoneChangeRequestData:
        return PhoneChangeRequestData(
            id=row.id,
            parent_id=row.parent_id,
            old_phone_hash=row.old_phone_hash,
            new_phone_hash=row.new_phone_hash,
            verification_code=row.verification_code,
            requested_at=cast("datetime", _aware(row.requested_at)),
            expires_at=cast("datetime", _aware(row.expires_at)),
            effective_at=_aware(row.effective_at),
            status=row.status,
            confirmed_at=_aware(row.confirmed_at),
            completed_at=_aware(row.completed_at),
            cancelled_at=_aware(row.cancelled_at),
        )


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_repo: PhoneChangeRepository | None = None


def get_phone_change_repository() -> PhoneChangeRepository:
    """Return the process-wide repo (default: in-memory)."""
    global _repo
    if _repo is None:
        _repo = InMemoryPhoneChangeRepository()
    return _repo


def set_phone_change_repository(repo: PhoneChangeRepository) -> None:
    """Override the process-wide repo (tests / DI)."""
    global _repo
    _repo = repo
