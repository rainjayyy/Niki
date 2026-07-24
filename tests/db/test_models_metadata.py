"""Asserts Base.metadata has exactly the 8 Niki tables registered.

This is a guard against adding/removing models without updating
Alembic migrations (§7.1.6). If this test fails, you likely need to
add a new Alembic migration in `alembic/versions/`.
"""

from __future__ import annotations

import pytest

from niki.db.base import Base

EXPECTED_TABLES = {
    "parents",
    "children",
    "account_verifications",
    "deletion_requests",
    "weekly_reports",
    "emotion_baselines",
    "memory_chunks",
    "treehole_summaries",
}


class TestMetadataShape:
    def test_has_eight_tables(self) -> None:
        actual = set(Base.metadata.tables.keys())
        assert actual == EXPECTED_TABLES, (
            f"schema drift: expected {EXPECTED_TABLES}, got {actual}"
        )

    def test_parents_has_status_index(self) -> None:
        idx_names = {i.name for i in Base.metadata.tables["parents"].indexes}
        assert "ix_parents_status" in idx_names
        assert "ix_parents_id_number_hash" in idx_names
        assert "ix_parents_phone_hash" in idx_names

    def test_children_has_parent_fk(self) -> None:
        cols = Base.metadata.tables["children"].columns
        fk_targets = {fk.column.table.name for fk in cols["parent_id"].foreign_keys}
        assert "parents" in fk_targets

    def test_verifications_unique_per_step(self) -> None:
        uqs = [c for c in Base.metadata.tables["account_verifications"].constraints
               if hasattr(c, "name") and c.name == "uq_verification_step"]
        assert len(uqs) == 1

    def test_treehole_summary_unique_scope(self) -> None:
        uqs = [c for c in Base.metadata.tables["treehole_summaries"].constraints
               if hasattr(c, "name") and c.name == "uq_treehole_summary_scope"]
        assert len(uqs) == 1

    @pytest.mark.parametrize(
        "table",
        sorted(EXPECTED_TABLES),
    )
    def test_all_tables_have_audit_timestamp(self, table: str) -> None:
        cols = {c.name for c in Base.metadata.tables[table].columns}
        # Audit timestamp may be `created_at`, or a domain-specific one.
        accepted = {
            "created_at",
            "requested_at",  # deletion_requests
            "generated_at",  # weekly_reports
            "recorded_at",   # emotion_baselines
        }
        assert cols & accepted, (
            f"{table} missing any audit timestamp; got: {cols}"
        )
