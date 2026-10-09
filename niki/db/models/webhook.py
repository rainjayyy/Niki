"""WebhookSubscription + WebhookDelivery ORM models (BE-API-008).

A parent account may register multiple HTTP webhook targets. Each
target receives signed POSTs for the event_types it subscribed to
(account.deleted, deletion.completed, safety.escalated, …). The
actual signing + retry is wired in
`niki.services.webhook.WebhookDispatcher`; this module is schema-only.

Two tables:

  webhook_subscriptions — one row per (owner, target_url).
  webhook_deliveries    — append-only log of dispatch attempts.
                          `UNIQUE (event_id, subscription_id)` makes
                          `dispatch()` idempotent on retry: a second
                          call with the same `event_id` is a no-op.

Why a per-row `failure_count` on the subscription
------------------------------------------------
We auto-disable a subscription after 5 consecutive failures so a
stuck target can't pile up unbounded retries. The counter lives on
the row so the disable decision is a single round-trip — we don't
need to query `webhook_deliveries` for "last 5 attempts" on every
dispatch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from niki.db.base import Base


# Event types we currently emit. Kept as a frozenset so the
# validator in `niki.api.webhooks` can do a single O(1) membership
# check rather than walking a list. New event types are added here
# AND to the validator simultaneously.
VALID_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "account.deleted",
        "deletion.completed",
        "deletion.requested",
        "safety.escalated",
        "safety.blocked",
        "audit.high_risk",
    }
)

# Hard cap on how many event_types a single subscription can
# request. 10 is generous (the spec ships 6 today) but stops a
# malicious client from setting up a wildcard-catch-all and
# forcing us to send every event to one URL.
MAX_EVENT_TYPES_PER_SUBSCRIPTION: int = 10

# After this many consecutive failed dispatches, the subscription
# is auto-disabled. Tied to the retry policy (3 attempts per
# dispatch) so a single transient blip doesn't trip it.
MAX_CONSECUTIVE_FAILURES: int = 5


def _uuid_str() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WebhookSubscription(Base):
    """One webhook target per parent.

    `secret` is generated server-side and returned ONCE on the
    `POST /webhooks` response. Subsequent reads (list / get /
    update / deliveries) MUST NOT include the secret — the
    `to_public_dict()` projection below enforces that.
    """

    __tablename__ = "webhook_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    owner_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # HMAC secret used to sign the request body. Stored in clear
    # so the dispatcher can re-derive the signature on each attempt;
    # see `niki.services.webhook.sign_payload` for the algorithm.
    # NEVER returned via the API after creation.
    secret: Mapped[str] = mapped_column(String(64), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(
        "event_types",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_delivery_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
    )

    __table_args__ = (
        # The parent-facing "show me my webhooks" query is
        # `(owner_id, is_active, created_at DESC)`. We index
        # `is_active` separately too so the dispatcher's "all
        # active subs matching event_type" lookup stays fast as
        # the table grows.
        Index(
            "ix_webhook_subs_owner_active",
            "owner_id",
            "is_active",
        ),
    )

    def to_public_dict(self) -> dict[str, Any]:
        """Public projection — NEVER includes `secret`.

        The dispatcher reads the secret off the ORM instance
        directly; callers from the API layer should ONLY use this
        helper. The raw `to_dict()` we don't define — a slip-up
        here would be a secret leak.
        """
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "target_url": self.target_url,
            "event_types": list(self.event_types or []),
            "is_active": self.is_active,
            "failure_count": self.failure_count,
            "last_delivery_at": (
                self.last_delivery_at.isoformat() if self.last_delivery_at else None
            ),
            "disabled_at": (self.disabled_at.isoformat() if self.disabled_at else None),
            "created_at": (self.created_at.isoformat() if self.created_at else None),
            "updated_at": (self.updated_at.isoformat() if self.updated_at else None),
        }

    def to_created_dict(self) -> dict[str, Any]:
        """Projection for the `POST /webhooks` response.

        Same as `to_public_dict` plus the `secret` field — this is
        the ONLY place the secret is ever returned to the API
        layer. After this point it lives in the DB only.
        """
        out = self.to_public_dict()
        out["secret"] = self.secret
        return out


class WebhookDelivery(Base):
    """One dispatch attempt (logical event × subscription).

    Idempotency is enforced at the schema level:
    `UNIQUE (event_id, subscription_id)`. The dispatcher's
    `INSERT ... ON CONFLICT DO NOTHING` (or the in-memory
    equivalent) makes a re-dispatch a no-op.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    subscription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Idempotency key. Typically the audit_logs.id of the event
    # that triggered the dispatch, but a fresh uuid4() is fine
    # when there's no source event.
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        "payload",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    # Denormalised so the deliveries history doesn't break when a
    # subscription is later updated or deleted.
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    # Set on the first 2xx response. Stays NULL on permanent
    # failure so `WHERE delivered_at IS NULL` is the "needs
    # follow-up" set.
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
    )

    __table_args__ = (
        Index(
            "ix_webhook_deliveries_sub_created",
            "subscription_id",
            "created_at",
        ),
        UniqueConstraint(
            "event_id",
            "subscription_id",
            name="uq_webhook_deliveries_event_sub",
        ),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subscription_id": self.subscription_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": dict(self.payload or {}),
            "target_url": self.target_url,
            "attempt_count": self.attempt_count,
            "last_attempt_at": (self.last_attempt_at.isoformat() if self.last_attempt_at else None),
            "last_status_code": self.last_status_code,
            "last_error": self.last_error,
            "delivered_at": (self.delivered_at.isoformat() if self.delivered_at else None),
            "created_at": (self.created_at.isoformat() if self.created_at else None),
        }
