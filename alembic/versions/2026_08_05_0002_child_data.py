"""child achievements, chat sessions, weekly report history

Revision ID: 2026_08_05_0002
Revises: 2026_08_05_0001
Create Date: 2026-08-05 15:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2026_08_05_0002"
down_revision = "2026_08_05_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "child_achievements",
        sa.Column("child_id", sa.String(36), primary_key=True),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column("unlocked", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("counters", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["parent_id"], ["parents.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_child_achievements_parent", "child_achievements", ["parent_id"])
    op.create_index("ix_child_achievements_child", "child_achievements", ["child_id"])

    op.create_table(
        "chat_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("child_id", sa.String(36), nullable=False),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column("title", sa.String(120), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_sec", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("topics", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("messages", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["parent_id"], ["parents.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_chat_sessions_child", "chat_sessions", ["child_id"])
    op.create_index("ix_chat_sessions_parent", "chat_sessions", ["parent_id"])

    op.create_table(
        "weekly_report_history",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("child_id", sa.String(36), nullable=False),
        sa.Column("week_start", sa.String(10), nullable=False),
        sa.Column("week_end", sa.String(10), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_weekly_report_history_child", "weekly_report_history", ["child_id"])


def downgrade() -> None:
    op.drop_table("weekly_report_history")
    op.drop_table("chat_sessions")
    op.drop_table("child_achievements")
