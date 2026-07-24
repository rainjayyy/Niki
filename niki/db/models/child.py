"""Child ORM model — child profile owned by a Parent."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from niki.db.base import Base

if TYPE_CHECKING:
    from niki.db.models.parent import Parent


class Child(Base):
    """A child profile under a parent account."""

    __tablename__ = "children"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID string
    parent_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("parents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    child_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    parent: Mapped[Parent] = relationship("Parent", back_populates="children")

    def __repr__(self) -> str:
        return f"<Child id={self.id} parent_id={self.parent_id} name={self.name!r}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "name": self.name,
            "birth_date": self.birth_date.isoformat() if self.birth_date else None,
            "avatar_url": self.avatar_url,
            "metadata": dict(self.child_metadata or {}),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
