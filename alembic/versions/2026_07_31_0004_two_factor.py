"""BE-AUTH-007: parent 2FA / TOTP config table.

Revision ID: 2026_07_31_0004
Revises: 2026_07_31_0003
Create Date: 2026-07-31 12:00:00

Adds `two_factor_configs`, one row per parent. The TOTP secret is
stored Fernet-encrypted; the column name signals that — never
plaintext. Backup codes live as a single bcrypt hash; the indices
of consumed codes are tracked separately so the same code can't
be redeemed twice.

Reversible: `downgrade()` drops the table; `upgrade()` recreates it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_07_31_0004"
down_revision: str | Sequence[str] | None = "2026_07_31_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "two_factor_configs",
        sa.Column("parent_id", sa.String(16), primary_key=True),
        # Fernet ciphertext — base64 token, never plaintext.
        sa.Column(
            "secret_encrypted",
            sa.String(512),
            nullable=False,
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "enabled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "backup_codes_hash",
            sa.String(255),
            nullable=True,
        ),
        sa.Column(
            "backup_codes_used",
            sa.JSON().with_variant(
                postgresql.JSONB(), "postgresql"
            ),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_two_factor_configs_enabled",
        "two_factor_configs",
        ["is_enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_two_factor_configs_enabled",
        table_name="two_factor_configs",
    )
    op.drop_table("two_factor_configs")