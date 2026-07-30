"""add 5 tables for BE-STORG-001-ext-B/C/D + BE-MEDIA-001 + BE-PUSH-001.

Revision ID: 2026_07_30_0001
Revises: 2026_07_24_0001
Create Date: 2026-07-30 14:00:00

BE-STORG-001-ext-F: surfaces the rest of the model surface
that the create_all() path was silently creating in dev /
tests but never had a corresponding Alembic migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_07_30_0001"
down_revision: str | Sequence[str] | None = "2026_07_24_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "treehole_aggregations",
        sa.Column("child_id", sa.String(36), nullable=False),
        sa.Column("window_kind", sa.String(16), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valence_sum", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("negative_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("positive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("neutral_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("intensity_sum", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "category_counts",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("first_engagement_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_engagement_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("child_id", "window_kind", "window_start"),
        sa.UniqueConstraint(
            "child_id", "window_kind", "window_start",
            name="uq_treehole_agg_scope",
        ),
        sa.ForeignKeyConstraint(["child_id"], ["children.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "auth_audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("actor_parent_id", sa.String(16), nullable=True, index=True),
        sa.Column("target_parent_id", sa.String(16), nullable=True),
        sa.Column("target_child_id", sa.String(36), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("endpoint", sa.String(128), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_index("ix_auth_audit_actor_time", "auth_audit_logs", ["actor_parent_id", "created_at"])

    op.create_table(
        "push_devices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parent_id", sa.String(16), nullable=False, index=True),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("push_token", sa.String(512), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("parent_id", "device_id", name="uq_push_device_parent_device"),
        sa.UniqueConstraint("push_token", name="uq_push_device_token"),
        sa.ForeignKeyConstraint(["parent_id"], ["parents.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "push_prefs",
        sa.Column("parent_id", sa.String(16), primary_key=True),
        sa.Column("quiet_start", sa.Integer(), nullable=True),
        sa.Column("quiet_end", sa.Integer(), nullable=True),
        sa.Column(
            "enabled_channels",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("high_risk_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["parents.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "dashboard_alerts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("alert_type", sa.String(32), nullable=False, index=True),
        sa.Column("child_id", sa.String(36), nullable=False, index=True),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_dashboard_alerts_child_type", "dashboard_alerts", ["child_id", "alert_type"])

    op.create_table(
        "tutoring_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("child_id", sa.String(36), nullable=False, index=True),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["child_id"], ["children.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "knowledge_queries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("child_id", sa.String(36), nullable=False, index=True),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["child_id"], ["children.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_queries")
    op.drop_table("tutoring_events")
    op.drop_table("dashboard_alerts")
    op.drop_table("push_prefs")
    op.drop_table("push_devices")
    op.drop_index("ix_auth_audit_actor_time", table_name="auth_audit_logs")
    op.drop_table("auth_audit_logs")
    op.drop_table("treehole_aggregations")
