"""Unit tests for niki.db.repositories.treehole_repo (BE-STORG-001-ext-B).

Both the InMemory and the Postgres (via SQLite-async) implementations
are exercised against the same contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from niki.db.models import TreeholeEngagement
from niki.db.repositories import (
    InMemoryTreeholeRepository,
    PostgresTreeholeRepository,
)
from niki.db.repositories.treehole_repo import _row_to_dict


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class TestInMemoryRepo:
    async def test_add_returns_uuid(self) -> None:
        repo = InMemoryTreeholeRepository()
        row_id = await repo.add(
            child_id="c1",
            parent_id="p1",
            encrypted_payload=b"ct",
            associated_data=b"aad",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        assert isinstance(row_id, str) and len(row_id) > 0

    async def test_add_then_list_returns_one(self) -> None:
        repo = InMemoryTreeholeRepository()
        await repo.add(
            child_id="c1",
            parent_id="p1",
            encrypted_payload=b"ct",
            associated_data=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        rows = await repo.list_for_child("c1")
        assert len(rows) == 1
        assert rows[0]["child_id"] == "c1"

    async def test_list_filters_by_child(self) -> None:
        repo = InMemoryTreeholeRepository()
        for c in ("c1", "c2", "c1"):
            await repo.add(
                child_id=c,
                parent_id="p1",
                encrypted_payload=b"ct",
                associated_data=None,
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
        assert len(await repo.list_for_child("c1")) == 2
        assert len(await repo.list_for_child("c2")) == 1
        assert len(await repo.list_for_child("c3")) == 0

    async def test_list_skips_expired(self) -> None:
        repo = InMemoryTreeholeRepository()
        await repo.add(
            child_id="c1",
            parent_id="p1",
            encrypted_payload=b"fresh",
            associated_data=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        await repo.add(
            child_id="c1",
            parent_id="p1",
            encrypted_payload=b"stale",
            associated_data=None,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        rows = await repo.list_for_child("c1")
        assert len(rows) == 1
        assert rows[0]["encrypted_payload"] == b"fresh"

    async def test_delete_expired_drops_only_stale(self) -> None:
        repo = InMemoryTreeholeRepository()
        await repo.add(
            child_id="c1",
            parent_id="p1",
            encrypted_payload=b"f",
            associated_data=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        await repo.add(
            child_id="c2",
            parent_id="p1",
            encrypted_payload=b"s",
            associated_data=None,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        n = await repo.delete_expired()
        assert n == 1
        assert len(await repo.list_all()) == 1

    async def test_delete_all_for_child(self) -> None:
        repo = InMemoryTreeholeRepository()
        for _ in range(3):
            await repo.add(
                child_id="c1",
                parent_id="p1",
                encrypted_payload=b"x",
                associated_data=None,
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
        await repo.add(
            child_id="c2",
            parent_id="p1",
            encrypted_payload=b"x",
            associated_data=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        n = await repo.delete_all_for_child("c1")
        assert n == 3
        # c2 remains
        assert len(await repo.list_all()) == 1

    async def test_delete_all_clears_repo(self) -> None:
        repo = InMemoryTreeholeRepository()
        for _ in range(5):
            await repo.add(
                child_id="c1",
                parent_id="p1",
                encrypted_payload=b"x",
                associated_data=None,
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
        n = await repo.delete_all()
        assert n == 5
        assert await repo.list_all() == []


# ---------------------------------------------------------------------------
# Postgres implementation (against SQLite-async for portability)
# ---------------------------------------------------------------------------


class TestPostgresRepoSqlite:
    """Smoke test the SQLAlchemy impl via in-memory SQLite.

    Production uses real Postgres; the SQL surface is identical for
    these operations (insert / select / delete by predicate).
    """

    async def test_round_trip_insert_select_delete(self, async_session_factory) -> None:
        async with async_session_factory() as session:
            repo = PostgresTreeholeRepository(session)
            await repo.add(
                child_id="c1",
                parent_id="p1",
                encrypted_payload=b"ct-bytes",
                associated_data=b"aad-bytes",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
            await session.commit()

        async with async_session_factory() as session:
            repo2 = PostgresTreeholeRepository(session)
            rows = await repo2.list_for_child("c1")
        assert len(rows) == 1
        row = rows[0]
        assert row["child_id"] == "c1"
        assert row["parent_id"] == "p1"
        assert row["encrypted_payload"] == b"ct-bytes"
        assert row["associated_data"] == b"aad-bytes"

        async with async_session_factory() as session:
            repo3 = PostgresTreeholeRepository(session)
            n = await repo3.delete_all_for_child("c1")
            assert n == 1
            await session.commit()
        async with async_session_factory() as session:
            assert await repo3.list_for_child("c1") == []


# ---------------------------------------------------------------------------
# _row_to_dict helper
# ---------------------------------------------------------------------------


def test_row_to_dict_converts_orm_to_plain_dict() -> None:
    from datetime import datetime, timezone

    row = TreeholeEngagement(
        id="x" * 36,
        child_id="c1",
        parent_id="p1",
        encrypted_payload=b"ct",
        associated_data=None,
        expires_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    d = _row_to_dict(row)
    assert d["id"] == "x" * 36
    assert d["child_id"] == "c1"
    assert d["encrypted_payload"] == b"ct"
    assert d["associated_data"] is None
    assert d["expires_at"] == datetime(2026, 8, 1, tzinfo=timezone.utc)
