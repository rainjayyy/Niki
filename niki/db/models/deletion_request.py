"""DeletionRequest — a 30-day cool-off window for account erasure (PRD #35).

The 30-day window is enforced by checking `requested_at + 30d > now`
at the moment complete_deletion is invoked.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from niki.db.base import Base

if TYPE_CHECKING:
    from niki.db.models.parent import Parent


class DeletionStatus(str, enum.Enum):
    """Status of a deletion request — mirrors lifecycle.DeletionStatus."""

    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class DeletionRequest(Base):
    """A deletion request with a 30-day cool-off window (PRD #35)."""

    __tablename__ = "deletion_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    parent_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("parents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[DeletionStatus] = mapped_column(
        Enum(
            DeletionStatus,
            name="deletion_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=DeletionStatus.REQUESTED,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    parent: Mapped[Parent] = relationship("Parent", back_populates="deletion_requests")

    def __repr__(self) -> str:
        return (
            f"<DeletionRequest parent_id={self.parent_id} status={self.status.value}>"
        )
