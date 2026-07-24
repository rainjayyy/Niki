"""EmotionBaseline — rolling baseline of a child's emotional state.

One row per observation; z-score over the trailing window is computed
in-memory by EmotionBaselineService (see niki/api/emotion.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from niki.db.base import Base


class EmotionBaseline(Base):
    """A single observation in a child's emotion baseline."""

    __tablename__ = "emotion_baselines"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    child_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_id: Mapped[str] = mapped_column(String(16), nullable=False)
    emotion_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # "chat" / "baseline"
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_emotion_baselines_child_recorded", "child_id", "recorded_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<EmotionBaseline child_id={self.child_id} "
            f"score={self.emotion_score:.2f} at={self.recorded_at}>"
        )
