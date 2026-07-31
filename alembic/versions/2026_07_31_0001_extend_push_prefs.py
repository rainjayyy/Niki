"""BE-PUSH-004: extend push_prefs with DND + per-severity + per-event-type mute.

Revision ID: 2026_07_31_0001
Revises: 2026_07_30_0002
Create Date: 2026-07-31 10:00:00

Adds 4 columns to `push_prefs`:
  - `dnd_enabled`       bool NOT NULL  default false
  - `dnd_until`         timestamptz NULL  (temporary DND expiry)
  - `severities`        JSON NOT NULL  default ["warning","critical"]
  - `muted_event_types` JSON NOT NULL  default []

Reversible: `downgrade()` drops the four columns in reverse order.
The DB-level default for `severities` is the JSON literal
`'["warning","critical"]'` so existing rows get the new default
without requiring a backfill script.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_07_31_0001"
down_revision: str | Sequence[str] | None = "2026_07_30_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "push_prefs",
        sa.Column(
            "dnd_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "push_prefs",
        sa.Column(
            "dnd_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "push_prefs",
        sa.Column(
            "severities",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'[\"warning\",\"critical\"]'::jsonb"),
        ),
    )
    op.add_column(
        "push_prefs",
        sa.Column(
            "muted_event_types",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("push_prefs", "muted_event_types")
    op.drop_column("push_prefs", "severities")
    op.drop_column("push_prefs", "dnd_until")
    op.drop_column("push_prefs", "dnd_enabled")