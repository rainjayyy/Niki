"""Parent ORM model — persists ParentAccount business object (PRD #30).

The business-layer `ParentAccount` (in niki/auth/account.py) keeps
sensitive raw fields out of the API; this ORM row stores the
canonical record, with id_number / phone saved as **hashes only**
and the raw values never reaching the DB.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from niki.db.base import Base

if TYPE_CHECKING:
    from niki.db.models.child import Child
    from niki.db.models.deletion_request import DeletionRequest


class ParentStatus(str, enum.Enum):
    """Lifecycle status of a parent account — mirrors AccountStatus."""

    PENDING = "pending"
    VERIFIED = "verified"
    SUSPENDED = "suspended"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"


class Parent(Base):
    """A parent's persistent account record."""

    __tablename__ = "parents"

    # Primary key — sha256(name:id_number)[:16], aligns with AccountLifecycle._make_id.
    id: Mapped[str] = mapped_column(String(16), primary_key=True)

    # Identifying fields — raw name stored for display; id_number / phone
    # NEVER stored raw — only their hashes (so we can detect duplicates
    # without holding PII).
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    id_number_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Lifecycle state
    status: Mapped[ParentStatus] = mapped_column(
        Enum(
            ParentStatus,
            name="account_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ParentStatus.PENDING,
        index=True,
    )

    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deletion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deletion_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Free-form metadata (city, marketing-optin, etc.)
    parent_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",  # keep DB column name `metadata` (avoids SQLAlchemy clash)
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )

    # Relationships (one-to-many; cascade handled at DB layer via FK ON DELETE CASCADE)
    children: Mapped[list[Child]] = relationship(
        "Child",
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    deletion_requests: Mapped[list[DeletionRequest]] = relationship(
        "DeletionRequest",
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # Indexes / constraints
    __table_args__ = (
        Index("ix_parents_id_number_hash", "id_number_hash", unique=True),
        Index("ix_parents_phone_hash", "phone_hash", unique=True),
        Index("ix_parents_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Parent id={self.id} name={self.name!r} status={self.status.value}>"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to API response shape (matches ParentAccount.to_dict)."""
        return {
            "id": self.id,
            "name": self.name,
            "id_number_masked": f"{self.id_number_hash[:6]}****",
            "phone_masked": f"{self.phone_hash[:3]}****",
            "email": self.email,
            "status": self.status.value,
            "is_verified": self.status == ParentStatus.VERIFIED,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "deletion_requested_at": (
                self.deletion_requested_at.isoformat()
                if self.deletion_requested_at
                else None
            ),
            "deletion_completed_at": (
                self.deletion_completed_at.isoformat()
                if self.deletion_completed_at
                else None
            ),
            "deletion_window_remaining_days": self.deletion_window_days(),
            "child_ids": [c.id for c in self.children],
            "metadata": dict(self.parent_metadata or {}),
        }

    def deletion_window_days(self) -> int | None:
        """Return days remaining in the 30-day deletion window, if active."""
        from datetime import timedelta

        if self.status != ParentStatus.DELETION_PENDING:
            return None
        if self.deletion_requested_at is None:
            return None
        elapsed = datetime.now(timezone.utc) - self.deletion_requested_at
        return max(0, 30 - elapsed.days)
