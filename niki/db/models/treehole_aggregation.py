"""TreeholeAggregation ORM model (BE-TREEHOLE-AGG, BE-TREEHOLE-AGG-rollout).

Maintains a per-child rolling aggregate of treehole engagements
so `aggregate_for_baseline()` is O(1) instead of having to
decrypt every engagement on every dashboard refresh.

BE-TREEHOLE-AGG-rollout: the model now supports multiple
window granularities in parallel:
    - "daily"  (1-day window;  re-anchored each calendar day)
    - "weekly" (7-day window; anchored to ISO Monday)
The composite primary key (child_id, window_kind, window_start)
keeps the two windows in independent rows so a single
`engage()` can update both with cheap upserts.

PIPL compliant: only structured emotion metadata is stored; the
original child text never enters this table — it remains in the
encrypted `treehole_engagements` payload until the 7-day purge.
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from niki.db.base import Base


# Window-kind enum. The DB column is a plain String for SQLite
# compat; the application layer validates with these constants.
WINDOW_KIND_DAILY: str = "daily"
WINDOW_KIND_WEEKLY: str = "weekly"
VALID_WINDOW_KINDS: tuple[str, ...] = (WINDOW_KIND_DAILY, WINDOW_KIND_WEEKLY)


class TreeholeAggregation(Base):
    """One rolling-window aggregate row per (child × window_kind × window_start)."""

    __tablename__ = "treehole_aggregations"

    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        primary_key=True,
    )
    window_kind: Mapped[str] = mapped_column(
        String(16),
        primary_key=True,
        nullable=False,
        default=WINDOW_KIND_WEEKLY,
    )
    window_start: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Σ (valence_value × intensity / 5). valence_value is
    # +0.5 for positive, 0.0 for neutral, -0.5 for negative.
    # mean_valence = valence_sum / count (O(1) at read time).
    valence_sum: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    negative_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    neutral_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    intensity_sum: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    category_counts: Mapped[dict[str, Any]] = mapped_column(
        "category_counts",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    first_engagement_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_engagement_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "child_id",
            "window_kind",
            "window_start",
            name="uq_treehole_agg_scope",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<TreeholeAggregation child={self.child_id} "
            f"kind={self.window_kind} window={self.window_start} "
            f"count={self.count}>"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the aggregate."""
        return {
            "child_id": self.child_id,
            "window_kind": self.window_kind,
            "window_start": self.window_start.isoformat(),
            "count": self.count,
            "valence_sum": self.valence_sum,
            "mean_valence": (self.valence_sum / self.count if self.count else 0.0),
            "negative_count": self.negative_count,
            "positive_count": self.positive_count,
            "neutral_count": self.neutral_count,
            "intensity_sum": self.intensity_sum,
            "category_counts": dict(self.category_counts or {}),
            "first_engagement_at": (
                self.first_engagement_at.isoformat() if self.first_engagement_at else None
            ),
            "last_engagement_at": (
                self.last_engagement_at.isoformat() if self.last_engagement_at else None
            ),
            "updated_at": (self.updated_at.isoformat() if self.updated_at else None),
        }
