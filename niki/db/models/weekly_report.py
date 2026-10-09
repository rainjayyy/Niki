"""WeeklyReport — parent-facing weekly summary for one child."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from niki.db.base import Base

if TYPE_CHECKING:
    pass  # No back-refs yet; report is owned by child.


class WeeklyReport(Base):
    """A weekly summary report shown in the parent dashboard."""

    __tablename__ = "weekly_reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    child_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    report_data: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index(
            "ix_weekly_reports_child_week",
            "child_id",
            "week_start",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return f"<WeeklyReport child_id={self.child_id} week={self.week_start}>"
