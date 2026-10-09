"""sync missing tables and columns with ORM models

Revision ID: 2026_08_05_0004
Revises: 2026_08_05_0003
Create Date: 2026-08-05 15:35:00
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_08_05_0004"
down_revision = "2026_08_05_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "emotion_baseline_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("date", sa.String(length=10), nullable=False),
        sa.Column("valence", sa.Float(), nullable=False),
        sa.Column("intensity", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_emotion_baseline_records_created_at", "emotion_baseline_records", ["created_at"])
    op.create_index("ix_emotion_baseline_records_date", "emotion_baseline_records", ["date"])
    op.create_index("ix_emotion_baseline_date_created", "emotion_baseline_records", ["date", "created_at"])

    op.create_table(
        "treehole_engagements",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("child_id", sa.String(length=36), nullable=False),
        sa.Column("parent_id", sa.String(length=16), nullable=False),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
        sa.Column("associated_data", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["parent_id"], ["parents.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_treehole_engagements_child_id", "treehole_engagements", ["child_id"])
    op.create_index("ix_treehole_engagements_expires_at", "treehole_engagements", ["expires_at"])
    op.create_index("ix_treehole_engagements_child_created", "treehole_engagements", ["child_id", "created_at"])

    op.add_column(
        "dashboard_alerts",
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_dashboard_alerts_read_at", "dashboard_alerts", ["read_at"])
    op.create_index("ix_dashboard_alerts_parent_read_created", "dashboard_alerts", ["parent_id", "read_at", "created_at"])

    op.alter_column("auth_audit_logs", "endpoint", type_=sa.String(length=256), existing_type=sa.String(length=128))
    op.alter_column("two_factor_configs", "backup_codes_hash", type_=sa.String(length=1024), existing_type=sa.String(length=255))


def downgrade() -> None:
    op.drop_column("dashboard_alerts", "read_at")
    op.drop_table("treehole_engagements")
    op.drop_table("emotion_baseline_records")
