"""MemoryChunk — long-term memory persistence (see memory/long_term).

This table is the durable back-end for ChromaDB; Chroma holds vectors
but the canonical text lives here. The two are kept in sync by the
memory pipeline (BE-.../memory/pipeline).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from niki.db.base import Base


class MemoryChunk(Base):
    """A single remembered fact or interaction tied to a child."""

    __tablename__ = "memory_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="chat")
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_memory_chunks_child_created", "child_id", "created_at"),
    )

    def __repr__(self) -> str:
        snippet = (self.content or "")[:30]
        return f"<MemoryChunk id={self.id} child_id={self.child_id} content={snippet!r}>"
