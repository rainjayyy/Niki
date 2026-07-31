"""PushDevice + PushPrefs ORM models (BE-PUSH-001 + BE-PUSH-004).

A parent account may bind multiple push-capable devices (iOS,
Android, Web, mini-program). Each device registers a vendor
push token. The actual delivery to vendor servers is wired in
BE-PUSH-002 (P1) — this ticket only handles the registration
and preferences surface.

BE-PUSH-004 extends `PushPrefs` with quiet-hours behaviour
(`dnd_enabled`, `dnd_until`), per-severity whitelist
(`severities`) and per-event-type mute (`muted_event_types`).
The semantics are implemented in `niki/api/push.py::is_quiet_now`
and `should_deliver`; this module is intentionally schema-only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from niki.db.base import Base


# Vendor channels supported by BE-PUSH-002 (P1). Registration
# validates against this set so an invalid channel fails fast at
# 400 instead of silently no-op at delivery time.
VALID_CHANNELS: frozenset[str] = frozenset(
    {"hms", "mipush", "oppo", "vivo", "flyme", "apns", "fcm", "web"}
)
VALID_PLATFORMS: frozenset[str] = frozenset(
    {"ios", "android", "web", "weapp", "flutter"}
)

# BE-PUSH-004: severity whitelist used by `PushPrefs.severities`.
# `info` events are normally muted by default — only escalate to
# the parent when severity ≥ `warning`.
VALID_SEVERITIES: frozenset[str] = frozenset(
    {"info", "warning", "critical"}
)

# Default severities a fresh parent gets on first GET /push/prefs
# — `info` is intentionally excluded so a new parent doesn't get
# spammed. They can opt in via PATCH /push/prefs.
DEFAULT_SEVERITIES: list[str] = ["warning", "critical"]


def _uuid_str() -> str:
    return str(uuid4())


class PushDevice(Base):
    """One registered push device per parent."""

    __tablename__ = "push_devices"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_str
    )
    parent_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("parents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Client-supplied UUID (matches `X-Device-Id` header).
    # UNIQUE so a re-register on the same device updates in place
    # rather than creating duplicates.
    device_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False
    )
    push_token: Mapped[str] = mapped_column(
        String(512), nullable=False
    )
    platform: Mapped[str] = mapped_column(
        String(16), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "parent_id", "device_id",
            name="uq_push_device_parent_device",
        ),
        UniqueConstraint(
            "push_token", name="uq_push_device_token"
        ),
        Index(
            "ix_push_devices_parent_enabled",
            "parent_id",
            "enabled",
        ),
    )

    def to_dict(self) -> dict:
        """Public projection — NEVER includes `push_token`."""
        return {
            "id": self.id,
            "device_id": self.device_id,
            "channel": self.channel,
            "platform": self.platform,
            "enabled": self.enabled,
            "last_active_at": (
                self.last_active_at.isoformat()
                if self.last_active_at else None
            ),
            "created_at": (
                self.created_at.isoformat()
                if self.created_at else None
            ),
        }


class PushPrefs(Base):
    """Per-parent notification preferences (PRD silent hours, PRD #29 high-risk gate).

    BE-PUSH-004 added:
      - `dnd_enabled`  — toggle for "do not disturb" (overrides quiet hours).
      - `dnd_until`    — optional expiry timestamp; if set and in the future,
                         DND is active only until that point. If `dnd_enabled`
                         is True and `dnd_until` is NULL, DND persists.
      - `severities`   — whitelist of severities to deliver. Default
                         `["warning", "critical"]` so `info` events are muted.
      - `muted_event_types` — explicit per-event-type mute list. Default `[]`.

    Quiet-hours semantics (wrap-around) live in
    `niki/api/push.py::is_quiet_now` / `should_deliver`; this module
    is intentionally schema-only.
    """

    __tablename__ = "push_prefs"

    parent_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("parents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    quiet_start: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    quiet_end: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    enabled_channels: Mapped[list[str]] = mapped_column(
        "enabled_channels",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    high_risk_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # --- BE-PUSH-004 additions ---
    dnd_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    dnd_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    severities: Mapped[list[str]] = mapped_column(
        "severities",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=lambda: list(DEFAULT_SEVERITIES),
    )
    muted_event_types: Mapped[list[str]] = mapped_column(
        "muted_event_types",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "parent_id": self.parent_id,
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
            "enabled_channels": list(self.enabled_channels or []),
            "high_risk_only": self.high_risk_only,
            "dnd_enabled": self.dnd_enabled,
            "dnd_until": (
                self.dnd_until.isoformat()
                if self.dnd_until else None
            ),
            "severities": list(self.severities or []),
            "muted_event_types": list(self.muted_event_types or []),
            "updated_at": (
                self.updated_at.isoformat()
                if self.updated_at else None
            ),
        }