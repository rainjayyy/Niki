"""BE-AUTH-006: phone-change request table + email columns on parents.

Revision ID: 2026_07_31_0005
Revises: 2026_07_31_0004
Create Date: 2026-07-31 15:30:00

Adds `phone_change_requests` (one row per `POST /auth/phone/change/request`).
The `status` column is a free-form String(32) — the application enforces
the strict enum (`PhoneChangeStatus`); the DB stays loose so adding a
new state never requires another migration.

Reversible: `downgrade()` drops the table AND the email columns added
in the same step. Tested by the `test_alembic_roundtrip` test in
`tests/auth/test_phone_change.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2026_07_31_0005"
down_revision: str | Sequence[str] | None = "2026_07_31_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- 1. New table: phone_change_requests ----
    op.create_table(
        "phone_change_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parent_id", sa.String(16), nullable=False),
        sa.Column("old_phone_hash", sa.String(64), nullable=False),
        sa.Column("new_phone_hash", sa.String(64), nullable=False),
        sa.Column("verification_code", sa.String(8), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "effective_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending_verification",
        ),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "cancelled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_phone_change_parent_status",
        "phone_change_requests",
        ["parent_id", "status"],
    )
    op.create_index(
        "ix_phone_change_effective_status",
        "phone_change_requests",
        ["effective_at", "status"],
    )
    op.create_index(
        "ix_phone_change_parent_id",
        "phone_change_requests",
        ["parent_id"],
    )
    op.create_index(
        "ix_phone_change_status",
        "phone_change_requests",
        ["status"],
    )

    # ---- 2. Add email columns to parents ----
    op.add_column(
        "parents",
        sa.Column("email_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "parents",
        sa.Column(
            "email_verified",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_parents_email_hash",
        "parents",
        ["email_hash"],
    )


def downgrade() -> None:
    # Reverse the email columns first.
    op.drop_index("ix_parents_email_hash", table_name="parents")
    op.drop_column("parents", "email_verified")
    op.drop_column("parents", "email_hash")

    # Then drop the new table.
    op.drop_index(
        "ix_phone_change_status", table_name="phone_change_requests"
    )
    op.drop_index(
        "ix_phone_change_parent_id", table_name="phone_change_requests"
    )
    op.drop_index(
        "ix_phone_change_effective_status",
        table_name="phone_change_requests",
    )
    op.drop_index(
        "ix_phone_change_parent_status",
        table_name="phone_change_requests",
    )
    op.drop_table("phone_change_requests")
