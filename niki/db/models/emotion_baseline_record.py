"""EmotionBaselineRecord ORM model (BE-STORG-001-ext-D)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from niki.db.base import Base


class EmotionBaselineRecord(Base):
    """A single emotion baseline entry (7-day rolling window of valences).

    BE-ISS-09: prior to this revision the table was *global* (one row
    per (date, source)) which caused the nightly pipeline to
    overwrite per-child baseline data — the Z-score anomaly detection
    (PRD #51-#52) computed a single global baseline instead of one
    per child. Adding the ``child_id`` column (indexed, nullable for
    backward-compat with rows that pre-date the migration) gives
    every child its own 7-day window.
    """

    __tablename__ = "emotion_baseline_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    child_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    valence: Mapped[float] = mapped_column(Float, nullable=False)
    intensity: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        Index(
            "ix_emotion_baseline_child_date_created",
            "child_id",
            "date",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<EmotionBaselineRecord id={self.id} child_id={self.child_id} "
            f"date={self.date} valence={self.valence:.2f}>"
        )
