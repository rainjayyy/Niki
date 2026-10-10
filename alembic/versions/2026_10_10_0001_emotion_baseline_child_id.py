"""add child_id to emotion_baseline_records (BE-ISS-09)

Per-child baseline rows. Prior to this revision the table was global
(one row per date) which caused the nightly pipeline to overwrite
per-child baseline data; the Z-score anomaly detection (PRD #51-#52)
now computes one 7-day window per child instead of a single global
one.

Revision ID: 2026_10_10_0001
Revises: 2026_08_05_0005
Create Date: 2026-10-10 12:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_10_10_0001"
down_revision = "2026_08_05_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "emotion_baseline_records",
        sa.Column("child_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_emotion_baseline_records_child_id",
        "emotion_baseline_records",
        ["child_id"],
    )
    # Composite index supports the per-child 7-day window query used
    # by EmotionBaseline.check_anomaly and the pipeline recalc.
    op.create_index(
        "ix_emotion_baseline_child_date_created",
        "emotion_baseline_records",
        ["child_id", "date", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_emotion_baseline_child_date_created",
        table_name="emotion_baseline_records",
    )
    op.drop_index(
        "ix_emotion_baseline_records_child_id",
        table_name="emotion_baseline_records",
    )
    op.drop_column("emotion_baseline_records", "child_id")
