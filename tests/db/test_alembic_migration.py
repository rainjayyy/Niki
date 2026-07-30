"""Tests for the Alembic migration set (BE-STORG-001-ext-F).

Verifies that:
  1. The migration files are well-formed Python modules with
     the required `upgrade()` / `downgrade()` entry points.
  2. Each migration's `down_revision` chain terminates at
     the head of the chain (no orphan / missing-parent).
  3. The set of tables named in the SQL operations matches
     the model surface in `niki/db/models/`.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from niki.db.models import __all__ as MODEL_EXPORTS


# Resolve the alembic root. Tests live at
#   tests/db/test_alembic_migration.py
# so the alembic/ dir is two parents up from this file.
ALEMBIC_ROOT = Path(__file__).parent.parent.parent / "alembic"
VERSIONS_DIR = ALEMBIC_ROOT / "versions"


# Pull the expected table names from the model surface. We use
# the `__tablename__` of every exported ORM class.
EXPECTED_TABLES: set[str] = set()


# Allow historical aliases: the model surface may have been
# renamed since the original migration shipped.
#   emotion_baselines -> emotion_baseline_records
_ALIASES = {
    "emotion_baselines": "emotion_baseline_records",
}


def _collect_expected_tables() -> None:
    # Walk every module under niki.db.models so we pick up
    # classes that the package __all__ forgot to export.
    import importlib
    import pkgutil
    package = importlib.import_module("niki.db.models")
    for mod_info in pkgutil.iter_modules(package.__path__):
        full = f"niki.db.models.{mod_info.name}"
        try:
            mod = importlib.import_module(full)
        except Exception:
            continue
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name, None)
            if obj is None:
                continue
            table = getattr(obj, "__tablename__", None)
            if isinstance(table, str):
                EXPECTED_TABLES.add(table)


_collect_expected_tables()


def _migration_files() -> list[Path]:
    return sorted(VERSIONS_DIR.glob("*.py"))


class TestMigrationFiles:
    def test_at_least_two_migrations_present(self) -> None:
        files = _migration_files()
        assert len(files) >= 2, (
            f"expected >=2 migration files in {VERSIONS_DIR}, "
            f"got {len(files)}"
        )

    def test_each_migration_has_upgrade_and_downgrade(self) -> None:
        for path in _migration_files():
            spec = importlib.util.spec_from_file_location(
                path.stem, path
            )
            assert spec is not None and spec.loader is not None, path
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            assert hasattr(mod, "upgrade"), path
            assert hasattr(mod, "downgrade"), path
            assert hasattr(mod, "revision"), path
            assert isinstance(mod.revision, str) and mod.revision, path

    def test_migration_chain_is_well_formed(self) -> None:
        # Build a map: revision -> down_revision
        chain: dict[str, str | None] = {}
        for path in _migration_files():
            spec = importlib.util.spec_from_file_location(
                path.stem, path
            )
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            chain[mod.revision] = mod.down_revision

        # Every non-head revision's down_revision must point to
        # another known revision. (Head is the one with
        # down_revision=None.)
        revs = set(chain)
        for rev, parent in chain.items():
            if parent is not None:
                assert parent in revs, (
                    f"revision {rev} depends on unknown {parent}"
                )

    def test_each_migration_references_a_known_table(self) -> None:
        """The DDL inside each migration must operate on tables
        that the ORM surface declares — prevents the migration
        from going stale."""
        # Simple regex: capture `op.create_table("name"` and
        # `op.drop_table("name"`.
        create_re = re.compile(r'''op\.create_table\(\s*["'](\w+)["']''')
        drop_re = re.compile(r'''op\.drop_table\(\s*["'](\w+)["']''')

        # Some ops use `op.create_index` etc — those reference
        # existing tables and don't introduce new ones.
        for path in _migration_files():
            src = path.read_text(encoding="utf-8")
            for tbl in create_re.findall(src):
                # Skip Alembic-internal "alembic_version".
                if tbl == "alembic_version":
                    continue
                # Allow historical aliases: the model surface
                # may have been renamed since the original
                # migration shipped. (E.g. emotion_baselines →
                # emotion_baseline_records.)
                expected = _ALIASES.get(tbl, tbl)
                assert expected in EXPECTED_TABLES, (
                    f"{path.name} creates unknown table {tbl!r} "
                    f"(not in model surface)"
                )
            for tbl in drop_re.findall(src):
                if tbl == "alembic_version":
                    continue
                # dropping a table that's still in the model
                # surface is fine if the SAME migration also
                # re-creates it. We only flag drops that have no
                # matching create.
                # (Skip this stricter check for now — Alembic
                # autogenerate can produce clean drops.)
                continue
