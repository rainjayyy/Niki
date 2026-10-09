"""add created_at to child data tables

Revision ID: 2026_08_05_0005
Revises: 2026_08_05_0004
Create Date: 2026-08-05 15:45:00
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_08_05_0005"
down_revision = "2026_08_05_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ["child_achievements", "letters", "weekly_report_history"]:
        op.add_column(
            table,
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )


def downgrade() -> None:
    for table in ["child_achievements", "letters", "weekly_report_history"]:
        op.drop_column(table, "created_at")
