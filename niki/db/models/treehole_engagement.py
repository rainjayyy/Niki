"""TreeholeEngagement ORM model (BE-STORG-001-ext-B).

Stores the per-engagement encrypted payload so PRD #64's
aggregation window can survive process restarts. Distinct from
`TreeholeSummary` which holds the *aggregated* baseline output.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import DateTime, Index, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from niki.db.base import Base


class TreeholeEngagement(Base):
    """A single treehole-mode engagement (PRD #62).

    Holds the encrypted payload (raw bytes, base64-decoded from the
    wire format) plus enough metadata to decrypt and aggregate.
    """

    __tablename__ = "treehole_engagements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    child_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_id: Mapped[str] = mapped_column(String(16), nullable=False)
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    associated_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index(
            "ix_treehole_engagements_child_created",
            "child_id",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<TreeholeEngagement id={self.id} child_id={self.child_id} "
            f"expires_at={self.expires_at}>"
        )
