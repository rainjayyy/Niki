"""AuthAuditLog ORM model (BE-AUTH-001-rollout-audit).

Captures lateral-auth violations (and other auth-related events)
for the security audit trail required by PIPL / 等保 2.0 三级.

Rows are append-only — no UPDATE / DELETE in production code.
A background job (out of scope) archives rows older than 6 months
to cold storage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from niki.db.base import Base


def _uuid_str() -> str:
    return str(uuid4())


class AuthAuditLog(Base):
    """A single auth-related audit event.

    Every row has an `actor_parent_id` (the parent on whose behalf
    the request was made, decoded from the JWT) and zero or more
    target_* columns describing the resource the request tried to
    touch.

    Use composite indexes for fast per-actor lookups.
    """

    __tablename__ = "auth_audit_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_str
    )
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="info"
    )
    actor_parent_id: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True, index=True
    )
    target_parent_id: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    target_child_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )
    endpoint: Mapped[str] = mapped_column(String(256), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    client_ip: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        Index(
            "ix_auth_audit_actor_time",
            "actor_parent_id",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AuthAuditLog id={self.id} event={self.event_type} "
            f"actor={self.actor_parent_id} target_child={self.target_child_id}>"
        )