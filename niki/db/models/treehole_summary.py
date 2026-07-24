"""TreeholeSummary — encrypted aggregate for treehole mode (PRD #41).

7-day auto-purge (TTL via `expires_at`). Encrypted at rest with the
shared family key (AES-256-GCM) — see niki/treehole/encryption.py.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Date, DateTime, Enum, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from niki.db.base import Base


class AggregatationPhase(str, enum.Enum):
    """Stage of aggregation for the parent-visible summary."""

    RAW = "raw"
    DAILY = "daily"
    WEEKLY = "weekly"


class TreeholeSummary(Base):
    """An encrypted, time-bounded emotional summary for the parent."""

    __tablename__ = "treehole_summaries"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[str] = mapped_column(String(16), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    phase: Mapped[AggregatationPhase] = mapped_column(
        Enum(
            AggregatationPhase,
            name="treehole_phase",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=AggregatationPhase.WEEKLY,
    )

    # AES-256-GCM encrypted payload
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    iv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    auth_tag: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Cluster pseudonymisation: sha256(child_id) — keeps child identity
    # out of analytical queries, but joins back via this hash.
    cluster_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # 7-day auto-purge TTL
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc) + timedelta(days=7),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "child_id", "week_start", "phase", name="uq_treehole_summary_scope"
        ),
    )

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at

    def __repr__(self) -> str:
        return (
            f"<TreeholeSummary id={self.id} child_id={self.child_id} "
            f"week={self.week_start} phase={self.phase.value}>"
        )

    def to_metadata(self) -> dict[str, Any]:
        """Non-sensitive metadata for the API (never exposes ciphertext)."""
        return {
            "id": self.id,
            "child_id": self.child_id,
            "week_start": self.week_start.isoformat(),
            "phase": self.phase.value,
            "cluster_key_hash": self.cluster_key_hash,
            "expires_at": self.expires_at.isoformat(),
            "created_at": self.created_at.isoformat(),
        }
