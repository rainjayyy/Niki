"""Dashboard persistence models (BE-STORG-001-ext-C).

Three tables hold the live-state data that DashboardService
previously kept in in-memory lists:
  - `dashboard_alerts`     — safety_valve + high_risk alerts
  - `tutoring_events`      — child-by-child tutoring interactions
  - `knowledge_queries`    — encyclopedia/knowledge lookups

Weekly reports (already covered by `weekly_reports` model) are not
re-defined here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import (
    DateTime,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON as GenericJSON

from niki.db.base import Base


def _uuid_str() -> str:
    return str(uuid4())


class DashboardAlert(Base):
    """A safety_valve or high_risk alert (composite table)."""

    __tablename__ = "dashboard_alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    child_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_id: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[Any] = mapped_column(
        GenericJSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # BE-PUSH-005: parent-facing "read" timestamp for the
    # notification center. Distinct from `delivered_at` (which
    # tracks vendor-side push delivery) — this is when the
    # parent actually opened/acked the notification in the UI.
    # Nullable so existing rows and existing readers (BE-RT-002
    # dashboard view) stay unaffected.
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (
        Index(
            "ix_dashboard_alerts_child_type",
            "child_id",
            "alert_type",
        ),
        # BE-PUSH-005: covers the notification-center's hot path
        # (`parent_id = ? AND read_at IS NULL ORDER BY created_at DESC`).
        Index(
            "ix_dashboard_alerts_parent_read_created",
            "parent_id",
            "read_at",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return f"<DashboardAlert id={self.id} type={self.alert_type} child={self.child_id}>"


class TutoringEvent(Base):
    """A tutoring interaction for one child."""

    __tablename__ = "tutoring_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    child_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_id: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[Any] = mapped_column(
        GenericJSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<TutoringEvent id={self.id} child={self.child_id}>"


class KnowledgeQuery(Base):
    """A knowledge / encyclopedia lookup event."""

    __tablename__ = "knowledge_queries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    child_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_id: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[Any] = mapped_column(
        GenericJSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<KnowledgeQuery id={self.id} child={self.child_id}>"
