"""AccountVerification — one row per signup step (PRD #30).

3 rows per parent: real_name / face / consent. Mirrors the
in-memory `SignupProgress.steps: list[SignupStep]` in lifecycle.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from niki.db.base import Base

# Step names — keep aligned with SignupProgress.steps.
STEP_REAL_NAME = "real_name"
STEP_FACE = "face"
STEP_CONSENT = "consent"
ALL_STEPS = (STEP_REAL_NAME, STEP_FACE, STEP_CONSENT)


class AccountVerification(Base):
    """A single verification step in the signup pipeline."""

    __tablename__ = "account_verifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    parent_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("parents.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_name: Mapped[str] = mapped_column(String(32), nullable=False)
    completed: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        UniqueConstraint("parent_id", "step_name", name="uq_verification_step"),
    )

    def __repr__(self) -> str:
        return (
            f"<AccountVerification parent_id={self.parent_id} "
            f"step={self.step_name} completed={self.completed}>"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.step_name,
            "completed": self.completed,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
        }

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(timezone.utc)
