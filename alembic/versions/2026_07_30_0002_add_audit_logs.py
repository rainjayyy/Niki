"""BE-COMP-004: unified `audit_logs` table.

Revision ID: 2026_07_30_0002
Revises: 2026_07_30_0001
Create Date: 2026-07-30 18:00:00

Adds the cross-domain audit trail. `auth_audit_logs` is deliberately
NOT dropped — the lateral-auth alerting path keeps reading it with a
narrow single-table index scan, and this table is dual-written for the
cross-domain "whole account history" view.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_07_30_0002"
down_revision: str | Sequence[str] | None = "2026_07_30_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("actor_parent_id", sa.String(16), nullable=True, index=True),
        sa.Column("target_parent_id", sa.String(16), nullable=True),
        sa.Column("target_child_id", sa.String(36), nullable=True),
        sa.Column("endpoint", sa.String(256), nullable=True),
        sa.Column("method", sa.String(8), nullable=True),
        sa.Column("reason", sa.String(512), nullable=False, server_default=""),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
    )
    op.create_index(
        "ix_audit_log_actor_time",
        "audit_logs",
        ["actor_parent_id", "created_at"],
    )
    op.create_index(
        "ix_audit_log_event_time",
        "audit_logs",
        ["event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_event_time", table_name="audit_logs")
    op.drop_index("ix_audit_log_actor_time", table_name="audit_logs")
    op.drop_table("audit_logs")
