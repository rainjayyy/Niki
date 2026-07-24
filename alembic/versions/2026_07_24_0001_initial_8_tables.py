"""initial 8 tables - parents / children / verifications / deletion / weekly /
emotion / memory / treehole.

Revision ID: 2026_07_24_0001
Revises:
Create Date: 2026-07-24 00:01:00

BE-STOR-001 — replaces in-memory dicts with persistent storage.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2026_07_24_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- enums (PostgreSQL only; SQLite stores as TEXT via fallback) ----
    account_status = postgresql.ENUM(
        "pending",
        "verified",
        "suspended",
        "deletion_pending",
        "deleted",
        name="account_status",
        create_type=False,  # created inline by Enum() on first use
    )
    deletion_status = postgresql.ENUM(
        "not_requested",
        "requested",
        "in_progress",
        "completed",
        name="deletion_status",
        create_type=False,
    )
    treehole_phase = postgresql.ENUM(
        "raw",
        "daily",
        "weekly",
        name="treehole_phase",
        create_type=False,
    )

    # ---- parents ----
    op.create_table(
        "parents",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("id_number_hash", sa.String(64), nullable=False),
        sa.Column("phone_hash", sa.String(64), nullable=False),
        sa.Column("email", sa.String(128), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "verified",
                "suspended",
                "deletion_pending",
                "deleted",
                name="account_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_index("ix_parents_id_number_hash", "parents", ["id_number_hash"], unique=True)
    op.create_index("ix_parents_phone_hash", "parents", ["phone_hash"], unique=True)
    op.create_index("ix_parents_status_created", "parents", ["status", "created_at"])
    op.create_index("ix_parents_status", "parents", ["status"])

    # ---- children ----
    op.create_table(
        "children",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "parent_id",
            sa.String(16),
            sa.ForeignKey("parents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("avatar_url", sa.String(512), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_children_parent_id", "children", ["parent_id"])

    # ---- account_verifications ----
    op.create_table(
        "account_verifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "parent_id",
            sa.String(16),
            sa.ForeignKey("parents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_name", sa.String(32), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(512), nullable=True),
        sa.UniqueConstraint("parent_id", "step_name", name="uq_verification_step"),
    )
    op.create_index(
        "ix_account_verifications_parent_id",
        "account_verifications",
        ["parent_id"],
    )

    # ---- deletion_requests ----
    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "parent_id",
            sa.String(16),
            sa.ForeignKey("parents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "not_requested",
                "requested",
                "in_progress",
                "completed",
                name="deletion_status",
            ),
            nullable=False,
            server_default="requested",
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_deletion_requests_parent_id", "deletion_requests", ["parent_id"])

    # ---- weekly_reports ----
    op.create_table(
        "weekly_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("child_id", sa.String(36), nullable=False),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column(
            "report_data",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("child_id", "week_start", name="ix_weekly_reports_child_week"),
    )
    op.create_index("ix_weekly_reports_child_id", "weekly_reports", ["child_id"])
    op.create_index("ix_weekly_reports_parent_id", "weekly_reports", ["parent_id"])

    # ---- emotion_baselines ----
    op.create_table(
        "emotion_baselines",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("child_id", sa.String(36), nullable=False),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column("emotion_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_emotion_baselines_child_recorded",
        "emotion_baselines",
        ["child_id", "recorded_at"],
    )
    op.create_index("ix_emotion_baselines_child_id", "emotion_baselines", ["child_id"])

    # ---- memory_chunks ----
    op.create_table(
        "memory_chunks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "child_id",
            sa.String(36),
            sa.ForeignKey("children.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "extra",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("source", sa.String(64), nullable=False, server_default="chat"),
        sa.Column("relevance_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_memory_chunks_child_created",
        "memory_chunks",
        ["child_id", "created_at"],
    )
    op.create_index("ix_memory_chunks_child_id", "memory_chunks", ["child_id"])

    # ---- treehole_summaries ----
    op.create_table(
        "treehole_summaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "child_id",
            sa.String(36),
            sa.ForeignKey("children.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column(
            "phase",
            sa.Enum("raw", "daily", "weekly", name="treehole_phase"),
            nullable=False,
            server_default="weekly",
        ),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
        sa.Column("iv", sa.LargeBinary(), nullable=False),
        sa.Column("auth_tag", sa.LargeBinary(), nullable=False),
        sa.Column("cluster_key_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "child_id",
            "week_start",
            "phase",
            name="uq_treehole_summary_scope",
        ),
    )
    op.create_index(
        "ix_treehole_summaries_child_id",
        "treehole_summaries",
        ["child_id"],
    )
    op.create_index(
        "ix_treehole_summaries_cluster_key_hash",
        "treehole_summaries",
        ["cluster_key_hash"],
    )
    op.create_index(
        "ix_treehole_summaries_expires_at",
        "treehole_summaries",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_treehole_summaries_expires_at", table_name="treehole_summaries")
    op.drop_index("ix_treehole_summaries_cluster_key_hash", table_name="treehole_summaries")
    op.drop_index("ix_treehole_summaries_child_id", table_name="treehole_summaries")
    op.drop_table("treehole_summaries")

    op.drop_index("ix_memory_chunks_child_id", table_name="memory_chunks")
    op.drop_index("ix_memory_chunks_child_created", table_name="memory_chunks")
    op.drop_table("memory_chunks")

    op.drop_index("ix_emotion_baselines_child_id", table_name="emotion_baselines")
    op.drop_index("ix_emotion_baselines_child_recorded", table_name="emotion_baselines")
    op.drop_table("emotion_baselines")

    op.drop_index("ix_weekly_reports_parent_id", table_name="weekly_reports")
    op.drop_index("ix_weekly_reports_child_id", table_name="weekly_reports")
    op.drop_table("weekly_reports")

    op.drop_index("ix_deletion_requests_parent_id", table_name="deletion_requests")
    op.drop_table("deletion_requests")

    op.drop_index(
        "ix_account_verifications_parent_id", table_name="account_verifications"
    )
    op.drop_table("account_verifications")

    op.drop_index("ix_children_parent_id", table_name="children")
    op.drop_table("children")

    op.drop_index("ix_parents_status", table_name="parents")
    op.drop_index("ix_parents_status_created", table_name="parents")
    op.drop_index("ix_parents_phone_hash", table_name="parents")
    op.drop_index("ix_parents_id_number_hash", table_name="parents")
    op.drop_table("parents")

    # PostgreSQL-only enum cleanup (no-op on SQLite).
    sa.Enum(name="treehole_phase").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="deletion_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="account_status").drop(op.get_bind(), checkfirst=True)
