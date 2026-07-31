"""PhoneChangeRequest ORM model — BE-AUTH-006 parent phone/email change flow.

A request is created when a logged-in parent kicks off the
"change my phone" flow. State machine:

    pending_verification --(SMS code verified)--> pending_cooling
    pending_verification --(10 min elapse)----> expired
    pending_cooling     --(request_id cancel)--> cancelled
    pending_cooling     --(7d cooling elapses / confirm-now)--> completed

The newly persisted phone hash only flips on `completed` —
anything before that is reversible. The cron sweep
(`PhoneChangeService.sweep_expired`) finds rows where
`status='pending_cooling' AND effective_at <= now()` and moves
them forward, swapping the phone hash on the parent.

This model deliberately stores the SMS verification_code on
the row — it is short-lived (10 min) and ownership is bounded
by the row itself. The column is a String(6), never indexed
(no PII leak via index).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from niki.db.base import Base


def _uuid_str() -> str:
    return str(uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PhoneChangeStatus(str, enum.Enum):
    """Explicit state-machine for phone-change requests."""

    PENDING_VERIFICATION = "pending_verification"
    PENDING_COOLING = "pending_cooling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


VALID_PHONE_CHANGE_STATUSES: tuple[str, ...] = tuple(
    s.value for s in PhoneChangeStatus
)


class PhoneChangeRequest(Base):
    """One row per `POST /auth/phone/change/request`."""

    __tablename__ = "phone_change_requests"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_str
    )
    parent_id: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True
    )
    old_phone_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    new_phone_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    # 6-digit SMS code, hashed in production (this is the mock
    # backend — see InMemoryEmailCodeService analog). Bounded by
    # expires_at; the SMS verify helper cleans it up.
    verification_code: Mapped[str] = mapped_column(
        String(8), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
    )
    # 10-minute SMS-code expiry window. Copied from settings at
    # request time so a config flip mid-flight doesn't snap-close
    # every open request.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 7 days after the SMS code was verified; sweep flips the
    # row to COMPLETED once `effective_at <= now()`.
    effective_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PhoneChangeStatus.PENDING_VERIFICATION.value,
        index=True,
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # "all open requests for this parent" — UI banner.
        Index(
            "ix_phone_change_parent_status",
            "parent_id",
            "status",
        ),
        # "rows whose cooling period elapsed and need sweeping".
        Index(
            "ix_phone_change_effective_status",
            "effective_at",
            "status",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return (
            f"<PhoneChangeRequest id={self.id} "
            f"parent_id={self.parent_id} status={self.status}>"
        )
