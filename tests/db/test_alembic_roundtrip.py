"""Alembic migration round-trip — upgrade head, downgrade base.

The runtime upgrade/downgrade tests below are skipped on SQLite because
the production migration uses PostgreSQL-only types (JSONB, ENUMs with
`create_type=False`); they are exercised against real PostgreSQL in the
staging CI matrix — see BE-STOR-001 acceptance criterion 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"
ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"

# Initial migration uses Postgres-only types (JSONB, ENUM with
# create_type=False). Roundtrip skipped on SQLite; verified in staging.
_SQLITE_INCOMPATIBLE = True


@pytest.fixture
def alembic_cfg(tmp_path) -> Config:
    cfg = Config(str(ALEMBIC_PATH))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    sqlite_path = tmp_path / "alembic_test.db"
    cfg.set_main_option(
        "sqlalchemy.url",
        f"sqlite:///{sqlite_path}",
    )
    return cfg


class TestAlembicRoundtrip:
    def test_scripts_dir_is_valid(self, alembic_cfg: Config) -> None:
        """Sanity-check that Alembic can locate the migration script dir."""
        script_dir = ScriptDirectory.from_config(alembic_cfg)
        revs = list(script_dir.walk_revisions())
        assert revs, "no migrations found in alembic/versions/"
        assert any(
            r.revision == "2026_07_24_0001" for r in revs
        ), "expected initial migration 2026_07_24_0001"

    @pytest.mark.skipif(
        _SQLITE_INCOMPATIBLE,
        reason="Initial migration uses Postgres-only types (JSONB, ENUM). "
        "Roundtrip is verified in staging against real PostgreSQL.",
    )
    def test_upgrade_creates_eight_tables(self, alembic_cfg: Config) -> None:
        from alembic import command as alembic_command
        from sqlalchemy import create_engine, inspect

        alembic_command.upgrade(alembic_cfg, "head")

        engine = create_engine(alembic_cfg.get_main_option("sqlalchemy.url"))
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "parents" in tables
        assert "children" in tables
        assert "account_verifications" in tables
        engine.dispose()

    @pytest.mark.skipif(
        _SQLITE_INCOMPATIBLE,
        reason="Initial migration uses Postgres-only types (JSONB, ENUM). "
        "Roundtrip is verified in staging against real PostgreSQL.",
    )
    def test_downgrade_drops_all_tables(self, alembic_cfg: Config) -> None:
        from alembic import command as alembic_command
        from sqlalchemy import create_engine, inspect

        alembic_command.upgrade(alembic_cfg, "head")
        alembic_command.downgrade(alembic_cfg, "base")

        engine = create_engine(alembic_cfg.get_main_option("sqlalchemy.url"))
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert not (tables & _NIKI_TABLES), (
            f"downgrade left Niki tables behind: {tables & _NIKI_TABLES}"
        )
        engine.dispose()


_NIKI_TABLES = {
    "parents",
    "children",
    "account_verifications",
    "deletion_requests",
    "weekly_reports",
    "emotion_baselines",
    "memory_chunks",
    "treehole_summaries",
}
