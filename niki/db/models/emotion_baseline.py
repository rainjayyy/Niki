"""EmotionBaselineRecord ORM model (BE-STORG-001-ext-D)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from niki.db.base import Base


class EmotionBaselineRecord(Base):
    """A single emotion baseline entry (7-day rolling window of valences)."""

    __tablename__ = "emotion_baseline_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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

    __table_args__ = (Index("ix_emotion_baseline_date_created", "date", "created_at"),)

    def __repr__(self) -> str:
        return f"<EmotionBaselineRecord id={self.id} date={self.date} valence={self.valence:.2f}>"
