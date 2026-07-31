"""TwoFactorRepository — persistence facade for 2FA / TOTP state (BE-AUTH-007).

Two implementations:

* `InMemoryTwoFactorRepository` — default; tests + dev fallback.
* `PostgresTwoFactorRepository` — production; `two_factor_configs` table.

Both expose the same ABC. Reads return the ORM row (Postgres) or
a lightweight dataclass (`_MemTwoFactorConfig`); the service layer
never has to care which it has.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models.two_factor import TwoFactorConfig

logger = structlog.get_logger(__name__)

__all__ = [
    "TwoFactorConfigData",
    "TwoFactorRepository",
    "InMemoryTwoFactorRepository",
    "PostgresTwoFactorRepository",
    "get_two_factor_repository",
    "set_two_factor_repository",
]


@dataclass
class TwoFactorConfigData:
    """Plain-data view of a TwoFactorConfig row.

    Returned by both impls so the service layer works against
    one shape regardless of backend.
    """

    parent_id: str
    secret_encrypted: str
    is_enabled: bool = False
    enabled_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    backup_codes_hash: Optional[str] = None
    backup_codes_used: list[int] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_public_dict(self) -> dict:
        """Public projection for /status endpoint — NEVER includes
        the encrypted secret."""
        return {
            "parent_id": self.parent_id,
            "is_enabled": self.is_enabled,
            "enabled_at": (
                self.enabled_at.isoformat()
                if self.enabled_at
                else None
            ),
            "last_used_at": (
                self.last_used_at.isoformat()
                if self.last_used_at
                else None
            ),
            "backup_codes_remaining": max(
                0, 10 - len(self.backup_codes_used or [])
            ),
        }


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class TwoFactorRepository(ABC):
    """Persistence facade for 2FA / TOTP state."""

    @abstractmethod
    async def get(self, parent_id: str) -> TwoFactorConfigData | None:
        """Return the row, or None if no enrollment yet."""

    @abstractmethod
    async def upsert(
        self,
        *,
        parent_id: str,
        secret_encrypted: str,
        is_enabled: bool,
        enabled_at: Optional[datetime],
        last_used_at: Optional[datetime],
        backup_codes_hash: Optional[str],
        backup_codes_used: list[int],
    ) -> TwoFactorConfigData:
        """Insert-or-update the row, returning the persisted state.

        Idempotent on parent_id; this is the only write method the
        service needs.
        """

    @abstractmethod
    async def delete(self, parent_id: str) -> None:
        """Hard-delete the row (used by `disable()`)."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryTwoFactorRepository(TwoFactorRepository):
    """Dict-backed in-memory 2FA repo.

    Mirrors the `InMemory*Repository` convention used elsewhere —
    per-process storage, no async lock needed because FastAPI runs
    handlers in a single event loop. The service layer wraps every
    write in its own lock if it cares about atomicity.
    """

    def __init__(self) -> None:
        self._rows: dict[str, TwoFactorConfigData] = {}

    async def get(self, parent_id: str) -> TwoFactorConfigData | None:
        row = self._rows.get(parent_id)
        return copy.deepcopy(row) if row else None

    async def upsert(
        self,
        *,
        parent_id: str,
        secret_encrypted: str,
        is_enabled: bool,
        enabled_at: Optional[datetime],
        last_used_at: Optional[datetime],
        backup_codes_hash: Optional[str],
        backup_codes_used: list[int],
    ) -> TwoFactorConfigData:
        now = datetime.now(timezone.utc)
        existing = self._rows.get(parent_id)
        if existing is None:
            data = TwoFactorConfigData(
                parent_id=parent_id,
                secret_encrypted=secret_encrypted,
                is_enabled=is_enabled,
                enabled_at=enabled_at,
                last_used_at=last_used_at,
                backup_codes_hash=backup_codes_hash,
                backup_codes_used=list(backup_codes_used),
                created_at=now,
                updated_at=now,
            )
        else:
            data = TwoFactorConfigData(
                parent_id=parent_id,
                secret_encrypted=secret_encrypted,
                is_enabled=is_enabled,
                enabled_at=enabled_at,
                last_used_at=last_used_at,
                backup_codes_hash=backup_codes_hash,
                backup_codes_used=list(backup_codes_used),
                created_at=existing.created_at or now,
                updated_at=now,
            )
        self._rows[parent_id] = data
        return copy.deepcopy(data)

    async def delete(self, parent_id: str) -> None:
        self._rows.pop(parent_id, None)


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresTwoFactorRepository(TwoFactorRepository):
    """TwoFactorRepository backed by Postgres via SQLAlchemy async."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, parent_id: str) -> TwoFactorConfigData | None:
        result = await self._session.execute(
            select(TwoFactorConfig).where(
                TwoFactorConfig.parent_id == parent_id
            )
        )
        row = result.scalar_one_or_none()
        return self._to_data(row) if row else None

    async def upsert(
        self,
        *,
        parent_id: str,
        secret_encrypted: str,
        is_enabled: bool,
        enabled_at: Optional[datetime],
        last_used_at: Optional[datetime],
        backup_codes_hash: Optional[str],
        backup_codes_used: list[int],
    ) -> TwoFactorConfigData:
        result = await self._session.execute(
            select(TwoFactorConfig).where(
                TwoFactorConfig.parent_id == parent_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = TwoFactorConfig(
                parent_id=parent_id,
                secret_encrypted=secret_encrypted,
                is_enabled=is_enabled,
                enabled_at=enabled_at,
                last_used_at=last_used_at,
                backup_codes_hash=backup_codes_hash,
                backup_codes_used=list(backup_codes_used),
            )
            self._session.add(row)
        else:
            row.secret_encrypted = secret_encrypted
            row.is_enabled = is_enabled
            row.enabled_at = enabled_at
            row.last_used_at = last_used_at
            row.backup_codes_hash = backup_codes_hash
            row.backup_codes_used = list(backup_codes_used)
        await self._session.flush()
        return self._to_data(row)  # type: ignore[return-value]

    async def delete(self, parent_id: str) -> None:
        from sqlalchemy import delete as sa_delete

        await self._session.execute(
            sa_delete(TwoFactorConfig).where(
                TwoFactorConfig.parent_id == parent_id
            )
        )

    @staticmethod
    def _to_data(row: TwoFactorConfig) -> TwoFactorConfigData:
        return TwoFactorConfigData(
            parent_id=row.parent_id,
            secret_encrypted=row.secret_encrypted,
            is_enabled=bool(row.is_enabled),
            enabled_at=_aware(row.enabled_at),
            last_used_at=_aware(row.last_used_at),
            backup_codes_hash=row.backup_codes_hash,
            backup_codes_used=list(row.backup_codes_used or []),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_repo: TwoFactorRepository | None = None


def get_two_factor_repository() -> TwoFactorRepository:
    """Return the process-wide repo (default: in-memory)."""
    global _repo
    if _repo is None:
        _repo = InMemoryTwoFactorRepository()
    return _repo


def set_two_factor_repository(repo: TwoFactorRepository) -> None:
    """Override the process-wide repo (tests / Postgres)."""
    global _repo
    _repo = repo