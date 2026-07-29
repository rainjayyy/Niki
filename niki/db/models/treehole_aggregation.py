"""TreeholeAggregation ORM model (BE-TREEHOLE-AGG).

Maintains a per-child rolling aggregate of treehole engagements
so `aggregate_for_baseline()` is O(1) instead of having to
decrypt every engagement on every dashboard refresh.

PIPL compliant: only structured emotion metadata is stored; the
original child text never enters this table — it remains in the
encrypted `treehole_engagements` payload until the 7-day purge
(after which both rows disappear together).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from niki.db.base import Base


class TreeholeAggregation(Base):
    """One rolling-window aggregate row per child."""

    __tablename__ = "treehole_aggregations"

    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        primary_key=True,
    )
    window_start: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False
    )
    count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Σ (valence_value × intensity / 5). valence_value is
    # +0.5 for positive, 0.0 for neutral, -0.5 for negative.
    # mean_valence = valence_sum / count (O(1) at read time).
    valence_sum: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    negative_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    positive_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    neutral_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    intensity_sum: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    category_counts: Mapped[dict[str, Any]] = mapped_column(
        "category_counts",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    first_engagement_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_engagement_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<TreeholeAggregation child={self.child_id} "
            f"window={self.window_start} count={self.count}>"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the aggregate."""
        return {
            "child_id": self.child_id,
            "window_start": self.window_start.isoformat(),
            "count": self.count,
            "valence_sum": self.valence_sum,
            "mean_valence": (
                self.valence_sum / self.count if self.count else 0.0
            ),
            "negative_count": self.negative_count,
            "positive_count": self.positive_count,
            "neutral_count": self.neutral_count,
            "intensity_sum": self.intensity_sum,
            "category_counts": dict(self.category_counts or {}),
            "first_engagement_at": (
                self.first_engagement_at.isoformat()
                if self.first_engagement_at else None
            ),
            "last_engagement_at": (
                self.last_engagement_at.isoformat()
                if self.last_engagement_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat()
                if self.updated_at else None
            ),
        }