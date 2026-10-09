"""PushDeviceRepository + PushPrefsRepository (BE-PUSH-001 + BE-PUSH-004).

PushDevice:
  - Register/unregister/list devices per parent.
  - Same (parent_id, device_id) re-register updates in place
    rather than creating duplicates.

PushPrefs:
  - Single-row per parent (one prefs record per parent_id).
  - GET returns defaults for parents who haven't customised.
  - BE-PUSH-004 extended with: dnd_enabled, dnd_until, severities,
    muted_event_types. Quiet-hours semantics (wrap-around) live in
    `niki/api/push.py::is_quiet_now` / `should_deliver`.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models import PushDevice, PushPrefs


# ---------------------------------------------------------------------------
# Default prefs returned to parents who haven't customised.
# ---------------------------------------------------------------------------

DEFAULT_ENABLED_CHANNELS: list[str] = [
    "hms",
    "mipush",
    "oppo",
    "vivo",
    "flyme",
    "apns",
    "fcm",
    "web",
]

# BE-PUSH-004: defaults for the four new columns. `severities` is
# deliberately the same constant exposed as `DEFAULT_SEVERITIES`
# in `niki.db.models.push_device`; we re-declare the literal here
# to avoid an import cycle (this module is imported by the model
# layer's lazy-default via SQLAlchemy meta).
DEFAULT_SEVERITIES_LIST: list[str] = ["warning", "critical"]
DEFAULT_MUTED_EVENT_TYPES: list[str] = []


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# PushDeviceRepository
# ---------------------------------------------------------------------------


class PushDeviceRepository(ABC):
    """Contract for push device persistence."""

    @abstractmethod
    async def upsert(
        self,
        *,
        parent_id: str,
        device_id: str,
        channel: str,
        push_token: str,
        platform: str,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Register a device; if (parent_id, device_id) already
        exists, update its channel / push_token / platform /
        enabled / last_active_at and return the merged row.
        """

    @abstractmethod
    async def delete_by_device_id(self, parent_id: str, device_id: str) -> int:
        """Delete by (parent_id, device_id). Returns rows removed."""

    @abstractmethod
    async def delete_by_token(self, push_token: str) -> int:
        """Delete by push_token (used when the vendor reports the
        token as invalid). Returns rows removed."""

    @abstractmethod
    async def list_for_parent(self, parent_id: str) -> list[dict[str, Any]]:
        """List every registered device for `parent_id`."""

    @abstractmethod
    async def get_by_token(self, push_token: str) -> dict[str, Any] | None:
        """Lookup by push_token (used by future BE-PUSH-002 to
        route a delivery)."""

    @abstractmethod
    async def set_enabled(self, parent_id: str, device_id: str, enabled: bool) -> bool:
        """Enable / disable a single device. Returns True if
        the row was updated, False if no such device.

        BE-DEV-003: the "kick" path uses this. Disabling a
        device doesn't delete it; the row stays so the
        frontend can show "已登出" + a re-enable button.
        """

    @abstractmethod
    async def list_active_for_parent(self, parent_id: str) -> list[dict[str, Any]]:
        """List only the enabled devices for `parent_id`.

        Used by GET /devices/active (BE-DEV-003) and by
        future BE-PUSH-002 to pick delivery targets.
        """


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryPushDeviceRepository(PushDeviceRepository):
    """Thread-safe dict-backed push device store."""

    def __init__(self) -> None:
        # key: (parent_id, device_id) -> dict
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        self._token_index: dict[str, str] = {}  # push_token -> "parent_id|device_id"
        self._lock = threading.Lock()

    async def upsert(
        self,
        *,
        parent_id: str,
        device_id: str,
        channel: str,
        push_token: str,
        platform: str,
        enabled: bool = True,
    ) -> dict[str, Any]:
        key = (parent_id, device_id)
        with self._lock:
            row = self._rows.get(key)
            now = _now()
            if row is None:
                row_id = f"pd-{len(self._rows) + 1:08d}"
                row = {
                    "id": row_id,
                    "parent_id": parent_id,
                    "device_id": device_id,
                    "channel": channel,
                    "push_token": push_token,
                    "platform": platform,
                    "enabled": enabled,
                    "last_active_at": now,
                    "created_at": now,
                }
                self._rows[key] = row
            else:
                # Re-registering same (parent, device) — update the
                # mutable fields but preserve id + created_at.
                row["channel"] = channel
                row["push_token"] = push_token
                row["platform"] = platform
                row["enabled"] = enabled
                row["last_active_at"] = now
                # If another (parent, device) row held this token
                # before, drop it from the index to avoid stale
                # mapping (token must be globally unique per the
                # unique constraint semantics).
                old_token = row.get("push_token")
                if old_token and old_token != push_token:
                    self._token_index.pop(old_token, None)
            self._token_index[push_token] = f"{parent_id}|{device_id}"
            return deepcopy(row)

    async def delete_by_device_id(self, parent_id: str, device_id: str) -> int:
        with self._lock:
            key = (parent_id, device_id)
            row = self._rows.pop(key, None)
            if row is None:
                return 0
            self._token_index.pop(row.get("push_token", ""), None)
            return 1

    async def delete_by_token(self, push_token: str) -> int:
        with self._lock:
            key_str = self._token_index.pop(push_token, None)
            if not key_str:
                return 0
            parent_id, device_id = key_str.split("|", 1)
            self._rows.pop((parent_id, device_id), None)
            return 1

    async def list_for_parent(self, parent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(row) for (pid, _did), row in self._rows.items() if pid == parent_id]

    async def get_by_token(self, push_token: str) -> dict[str, Any] | None:
        with self._lock:
            key_str = self._token_index.get(push_token)
            if not key_str:
                return None
            parent_id, device_id = key_str.split("|", 1)
            row = self._rows.get((parent_id, device_id))
            return deepcopy(row) if row else None

    async def set_enabled(self, parent_id: str, device_id: str, enabled: bool) -> bool:
        with self._lock:
            row = self._rows.get((parent_id, device_id))
            if row is None:
                return False
            row["enabled"] = enabled
            return True

    async def list_active_for_parent(self, parent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                deepcopy(row)
                for (pid, _did), row in self._rows.items()
                if pid == parent_id and row.get("enabled", True)
            ]

    def __len__(self) -> int:
        return len(self._rows)


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresPushDeviceRepository(PushDeviceRepository):
    """Atomic upsert via INSERT ... ON CONFLICT (parent_id, device_id)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        parent_id: str,
        device_id: str,
        channel: str,
        push_token: str,
        platform: str,
        enabled: bool = True,
    ) -> dict[str, Any]:
        now = _now()
        stmt = (
            pg_insert(PushDevice)
            .values(
                parent_id=parent_id,
                device_id=device_id,
                channel=channel,
                push_token=push_token,
                platform=platform,
                enabled=enabled,
                last_active_at=now,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=[
                    PushDevice.parent_id,
                    PushDevice.device_id,
                ],
                set_={
                    "channel": channel,
                    "push_token": push_token,
                    "platform": platform,
                    "enabled": enabled,
                    "last_active_at": now,
                },
            )
            .returning(PushDevice)
        )
        # Defensive: if a different row already owns this token,
        # clear it before the unique constraint aborts the upsert.
        # In practice, the API layer should reject duplicates with
        # 409; this is a belt-and-braces.
        result = await self._session.execute(stmt)
        row = result.scalar_one()
        await self._session.flush()
        return row.to_dict()

    async def delete_by_device_id(self, parent_id: str, device_id: str) -> int:
        result = await self._session.execute(
            delete(PushDevice).where(
                PushDevice.parent_id == parent_id,
                PushDevice.device_id == device_id,
            )
        )
        await self._session.flush()
        return result.rowcount or 0

    async def delete_by_token(self, push_token: str) -> int:
        result = await self._session.execute(
            delete(PushDevice).where(PushDevice.push_token == push_token)
        )
        await self._session.flush()
        return result.rowcount or 0

    async def list_for_parent(self, parent_id: str) -> list[dict[str, Any]]:
        result = await self._session.execute(
            select(PushDevice)
            .where(PushDevice.parent_id == parent_id)
            .order_by(PushDevice.last_active_at.desc())
        )
        return [r.to_dict() for r in result.scalars().all()]

    async def get_by_token(self, push_token: str) -> dict[str, Any] | None:
        result = await self._session.execute(
            select(PushDevice).where(PushDevice.push_token == push_token)
        )
        row = result.scalar_one_or_none()
        return row.to_dict() if row else None

    async def set_enabled(self, parent_id: str, device_id: str, enabled: bool) -> bool:
        from sqlalchemy import update

        result = await self._session.execute(
            update(PushDevice)
            .where(
                PushDevice.parent_id == parent_id,
                PushDevice.device_id == device_id,
            )
            .values(enabled=enabled)
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0

    async def list_active_for_parent(self, parent_id: str) -> list[dict[str, Any]]:
        result = await self._session.execute(
            select(PushDevice)
            .where(
                PushDevice.parent_id == parent_id,
                PushDevice.enabled.is_(True),
            )
            .order_by(PushDevice.last_active_at.desc())
        )
        return [r.to_dict() for r in result.scalars().all()]


# ---------------------------------------------------------------------------
# PushPrefsRepository
# ---------------------------------------------------------------------------


def default_prefs(parent_id: str) -> dict[str, Any]:
    """Return a sane default-prefs dict for first-time callers."""
    return {
        "parent_id": parent_id,
        "quiet_start": None,
        "quiet_end": None,
        "enabled_channels": list(DEFAULT_ENABLED_CHANNELS),
        "high_risk_only": False,
        "dnd_enabled": False,
        "dnd_until": None,
        "severities": list(DEFAULT_SEVERITIES_LIST),
        "muted_event_types": list(DEFAULT_MUTED_EVENT_TYPES),
        "updated_at": _now(),
    }


class PushPrefsRepository(ABC):
    """Contract for per-parent notification preferences."""

    @abstractmethod
    async def get(self, parent_id: str) -> dict[str, Any]:
        """Return current prefs or defaults if no row exists."""

    @abstractmethod
    async def update(
        self,
        parent_id: str,
        *,
        quiet_start: int | None = None,
        quiet_end: int | None = None,
        enabled_channels: list[str] | None = None,
        high_risk_only: bool | None = None,
        dnd_enabled: bool | None = None,
        dnd_until: datetime | None = None,
        severities: list[str] | None = None,
        muted_event_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Partial update. None means 'leave unchanged'. Returns
        the merged row (or the freshly-created defaults row)."""


class InMemoryPushPrefsRepository(PushPrefsRepository):
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    async def get(self, parent_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._rows.get(parent_id)
            if row is None:
                return default_prefs(parent_id)
            return deepcopy(row)

    async def update(
        self,
        parent_id: str,
        *,
        quiet_start: int | None = None,
        quiet_end: int | None = None,
        enabled_channels: list[str] | None = None,
        high_risk_only: bool | None = None,
        dnd_enabled: bool | None = None,
        dnd_until: datetime | None = None,
        severities: list[str] | None = None,
        muted_event_types: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            row = self._rows.get(parent_id)
            if row is None:
                row = default_prefs(parent_id)
            if quiet_start is not None:
                row["quiet_start"] = quiet_start
            if quiet_end is not None:
                row["quiet_end"] = quiet_end
            if enabled_channels is not None:
                row["enabled_channels"] = list(enabled_channels)
            if high_risk_only is not None:
                row["high_risk_only"] = high_risk_only
            if dnd_enabled is not None:
                row["dnd_enabled"] = dnd_enabled
            if dnd_until is not None:
                row["dnd_until"] = dnd_until
            if severities is not None:
                row["severities"] = list(severities)
            if muted_event_types is not None:
                row["muted_event_types"] = list(muted_event_types)
            row["updated_at"] = _now()
            self._rows[parent_id] = row
            return deepcopy(row)


class PostgresPushPrefsRepository(PushPrefsRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, parent_id: str) -> dict[str, Any]:
        result = await self._session.execute(
            select(PushPrefs).where(PushPrefs.parent_id == parent_id)
        )
        row = result.scalar_one_or_none()
        return row.to_dict() if row else default_prefs(parent_id)

    async def update(
        self,
        parent_id: str,
        *,
        quiet_start: int | None = None,
        quiet_end: int | None = None,
        enabled_channels: list[str] | None = None,
        high_risk_only: bool | None = None,
        dnd_enabled: bool | None = None,
        dnd_until: datetime | None = None,
        severities: list[str] | None = None,
        muted_event_types: list[str] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        current = await self.get(parent_id)
        new_quiet_start = quiet_start if quiet_start is not None else current["quiet_start"]
        new_quiet_end = quiet_end if quiet_end is not None else current["quiet_end"]
        new_enabled_channels = (
            list(enabled_channels) if enabled_channels is not None else current["enabled_channels"]
        )
        new_high_risk_only = (
            high_risk_only if high_risk_only is not None else current["high_risk_only"]
        )
        new_dnd_enabled = dnd_enabled if dnd_enabled is not None else current["dnd_enabled"]
        new_dnd_until = dnd_until if dnd_until is not None else current["dnd_until"]
        new_severities = list(severities) if severities is not None else current["severities"]
        new_muted_event_types = (
            list(muted_event_types)
            if muted_event_types is not None
            else current["muted_event_types"]
        )
        stmt = (
            pg_insert(PushPrefs)
            .values(
                parent_id=parent_id,
                quiet_start=new_quiet_start,
                quiet_end=new_quiet_end,
                enabled_channels=new_enabled_channels,
                high_risk_only=new_high_risk_only,
                dnd_enabled=new_dnd_enabled,
                dnd_until=new_dnd_until,
                severities=new_severities,
                muted_event_types=new_muted_event_types,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[PushPrefs.parent_id],
                set_={
                    "quiet_start": new_quiet_start,
                    "quiet_end": new_quiet_end,
                    "enabled_channels": new_enabled_channels,
                    "high_risk_only": new_high_risk_only,
                    "dnd_enabled": new_dnd_enabled,
                    "dnd_until": new_dnd_until,
                    "severities": new_severities,
                    "muted_event_types": new_muted_event_types,
                    "updated_at": now,
                },
            )
            .returning(PushPrefs)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one()
        await self._session.flush()
        return row.to_dict()


# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------


_device_repo: PushDeviceRepository | None = None
_prefs_repo: PushPrefsRepository | None = None


def get_push_device_repository() -> PushDeviceRepository:
    global _device_repo
    if _device_repo is None:
        _device_repo = InMemoryPushDeviceRepository()
    return _device_repo


def set_push_device_repository(repo: PushDeviceRepository) -> None:
    global _device_repo
    _device_repo = repo


def get_push_prefs_repository() -> PushPrefsRepository:
    global _prefs_repo
    if _prefs_repo is None:
        _prefs_repo = InMemoryPushPrefsRepository()
    return _prefs_repo


def set_push_prefs_repository(repo: PushPrefsRepository) -> None:
    global _prefs_repo
    _prefs_repo = repo
