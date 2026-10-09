"""create letters table

Revision ID: 2026_08_05_0003
Revises: 2026_08_05_0002
Create Date: 2026-08-05 15:30:00
"""
from alembic import op
import sqlalchemy as sa

revision = "2026_08_05_0003"
down_revision = "2026_08_05_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "letters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("child_id", sa.String(36), nullable=False),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["parent_id"], ["parents.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_letters_child", "letters", ["child_id"])
    op.create_index("ix_letters_parent", "letters", ["parent_id"])


def downgrade() -> None:
    op.drop_table("letters")
