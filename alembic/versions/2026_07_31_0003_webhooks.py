"""BE-API-008: webhook_subscriptions + webhook_deliveries tables.

Revision ID: 2026_07_31_0003
Revises: 2026_07_31_0001
Create Date: 2026-07-31 11:00:00

Adds two tables for the webhook callback mechanism. `secret` on
`webhook_subscriptions` is the HMAC secret used to sign outbound
request bodies — generated server-side via `secrets.token_urlsafe(32)`
and returned ONLY in the `POST /webhooks` response (never on
list / get).

`webhook_deliveries.event_id` is the idempotency key: a second
dispatch with the same `(event_id, subscription_id)` pair is a
no-op via the UNIQUE constraint.

Reversible: downgrade() drops both tables in reverse order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_07_31_0003"
down_revision: str | Sequence[str] | None = "2026_07_31_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(16), nullable=False, index=True),
        sa.Column("target_url", sa.String(2048), nullable=False),
        sa.Column("secret", sa.String(64), nullable=False),
        sa.Column(
            "event_types",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "last_delivery_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "disabled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_webhook_subs_owner_active",
        "webhook_subscriptions",
        ["owner_id", "is_active"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.String(36),
            sa.ForeignKey(
                "webhook_subscriptions.id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("target_url", sa.String(2048), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_status_code",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "last_error",
            sa.String(1024),
            nullable=True,
        ),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_sub_created",
        "webhook_deliveries",
        ["subscription_id", "created_at"],
    )
    op.create_unique_constraint(
        "uq_webhook_deliveries_event_sub",
        "webhook_deliveries",
        ["event_id", "subscription_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_webhook_deliveries_event_sub", "webhook_deliveries"
    )
    op.drop_index(
        "ix_webhook_deliveries_sub_created", table_name="webhook_deliveries"
    )
    op.drop_table("webhook_deliveries")
    op.drop_index(
        "ix_webhook_subs_owner_active", table_name="webhook_subscriptions"
    )
    op.drop_table("webhook_subscriptions")
