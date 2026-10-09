"""add created_at to account_verifications

Revision ID: 2026_08_05_0001
Revises: 2026_07_31_0005
Create Date: 2026-08-05 14:46:00
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_08_05_0001"
down_revision = "2026_07_31_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_verifications",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("account_verifications", "created_at")
