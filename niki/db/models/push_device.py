"""PushDevice + PushPrefs ORM models (BE-PUSH-001).

A parent account may bind multiple push-capable devices (iOS,
Android, Web, mini-program). Each device registers a vendor
push token. The actual delivery to vendor servers is wired in
BE-PUSH-002 (P1) — this ticket only handles the registration
and preferences surface.
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
    """Per-parent notification preferences (PRD silent hours, PRD #29 high-risk gate)."""

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
            "updated_at": (
                self.updated_at.isoformat()
                if self.updated_at else None
            ),
        }