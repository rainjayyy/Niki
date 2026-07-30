"""AuditLog ORM model — the unified cross-domain audit trail (BE-COMP-004).

`auth_audit_logs` (BE-AUTH-001-rollout-audit) captures lateral-auth
violations only. Safety blocks, consent signatures, deletion requests
and login events were structlog-only, so a parent asking "show me
everything that happened on my account" had no single source to query.

This table is that source. Every compliance-relevant domain event
funnels through `niki.services.audit_log.AuditLogService.log_event`
and lands here.

Design notes
------------
- **Append-only.** No UPDATE / DELETE in production code. A retention
  job (out of scope) archives rows older than 6 months.
- **No PII.** Deliberately narrow: ids, endpoints, reasons and a
  metadata blob. Never child utterances, never a parent phone number,
  never a signature image. PIPL data-minimisation applies — this table
  is queried by support staff far more often than the domain tables.
- **Dual-write, not migration.** `auth_audit_logs` stays: the
  lateral-auth alerting path reads it with a single-table index scan
  and must not pay for a wider, hotter table. See BE-COMP-004 §设计.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from niki.db.base import Base


def _uuid_str() -> str:
    return str(uuid4())


# Severity ladder, shared with `auth_audit_repo` so a single alerting
# rule can span both tables.
SEVERITY_INFO: str = "info"
SEVERITY_WARNING: str = "warning"
SEVERITY_CRITICAL: str = "critical"
VALID_SEVERITIES: tuple[str, ...] = (
    SEVERITY_INFO,
    SEVERITY_WARNING,
    SEVERITY_CRITICAL,
)

# Well-known event types. The column is a free-form String so a new
# domain can start logging without a migration; these constants exist
# so call sites don't spell the same event two different ways.
EVENT_CONSENT_SIGNED: str = "consent_signed"
EVENT_DELETION_REQUESTED: str = "deletion_requested"
EVENT_DELETION_COMPLETED: str = "deletion_completed"
EVENT_SAFETY_BLOCK: str = "safety_block"
EVENT_HIGH_RISK_RECORDED: str = "high_risk_recorded"
EVENT_LATERAL_AUTH_VIOLATION: str = "lateral_auth_violation"
EVENT_SMS_SENT: str = "sms_sent"
EVENT_LOGIN_SUCCESS: str = "login_success"


class AuditLog(Base):
    """One cross-domain audit event.

    `actor_parent_id` is who did it (the JWT subject, when there is
    one); the `target_*` columns say what was touched. Both are
    nullable because some events are system-initiated (a retention
    sweep) and some happen pre-authentication (`sms_sent`).
    """

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_str
    )
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SEVERITY_INFO
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
    endpoint: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    method: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True
    )
    reason: Mapped[str] = mapped_column(
        String(512), nullable=False, default=""
    )
    # `metadata` is reserved on the SQLAlchemy declarative class, so
    # the Python attribute is `event_metadata` while the DB column
    # keeps the name the ticket specifies.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
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
        # "everything on my account, newest first" — the parent-facing
        # /admin/audit query.
        Index(
            "ix_audit_log_actor_time",
            "actor_parent_id",
            "created_at",
        ),
        # "every safety_block in the last 24h" — the ops dashboards.
        Index(
            "ix_audit_log_event_time",
            "event_type",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} event={self.event_type} "
            f"severity={self.severity} actor={self.actor_parent_id}>"
        )
