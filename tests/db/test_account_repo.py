"""Unit tests for `PostgresAccountRepository` against in-memory SQLite.

Covers the CRUD surface that AccountLifecycle relies on:
- create() seeds PENDING + 3 verifications
- get() round-trips to_business() mask fields
- mark_verification() / set_verified() / set_deletion_pending() /
  set_deleted() update state correctly
- delete() cascades to verifications
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from niki.auth.account import AccountStatus
from niki.db.models import AccountVerification, Parent, ParentStatus
from niki.db.repositories import PostgresAccountRepository


pytestmark = pytest.mark.asyncio


class TestCreate:
    async def test_create_seeds_pending_and_3_steps(self, session) -> None:
        repo = PostgresAccountRepository(session)
        acc = await repo.create(
            parent_id="abc123def456ab78",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
            email=None,
        )
        await session.commit()

        assert acc.status == AccountStatus.PENDING

        # 3 verification rows
        verifs = (
            await session.execute(
                select(AccountVerification).where(
                    AccountVerification.parent_id == "abc123def456ab78"
                )
            )
        ).scalars().all()
        assert len(verifs) == 3
        names = {v.step_name for v in verifs}
        assert names == {"real_name", "face", "consent"}
        assert all(v.completed is False for v in verifs)


class TestGet:
    async def test_get_returns_business_masked(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="abc123def456ab78",
            name="李四",
            id_number="110101199003078811",
            phone="13800138000",
            email="li@example.com",
        )
        await session.commit()

        fetched = await repo.get("abc123def456ab78")
        assert fetched is not None
        assert fetched.name == "李四"
        assert fetched.email == "li@example.com"
        assert fetched.id_number_masked.endswith("****")
        assert fetched.phone_masked.endswith("****")
        # Masked fields must NOT expose the raw input.
        assert "8811" not in fetched.id_number_masked
        assert "8000" not in fetched.phone_masked

    async def test_get_unknown_returns_none(self, session) -> None:
        repo = PostgresAccountRepository(session)
        assert await repo.get("nonexistent") is None


class TestProgress:
    async def test_progress_has_three_steps(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="abc123def456ab78",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await session.commit()

        progress = await repo.get_progress("abc123def456ab78")
        assert progress is not None
        step_names = [s.name for s in progress.steps]
        assert step_names == ["real_name", "face", "consent"]


class TestVerification:
    async def test_mark_verification_completes_step(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="abc123def456ab78",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await session.commit()

        await repo.mark_verification(
            "abc123def456ab78", "real_name", completed=True
        )
        await session.commit()

        progress = await repo.get_progress("abc123def456ab78")
        real_name = next(s for s in progress.steps if s.name == "real_name")
        assert real_name.completed is True
        assert real_name.completed_at is not None

    async def test_mark_verification_records_error(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="abc123def456ab78",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await session.commit()

        await repo.mark_verification(
            "abc123def456ab78",
            "face",
            completed=False,
            error="liveness check failed",
        )
        await session.commit()

        progress = await repo.get_progress("abc123def456ab78")
        face = next(s for s in progress.steps if s.name == "face")
        assert face.completed is False
        assert face.error == "liveness check failed"


class TestStatusTransitions:
    async def test_set_verified_changes_status(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="abc123def456ab78",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await session.commit()

        await repo.set_verified("abc123def456ab78")
        await session.commit()

        acc = await repo.get("abc123def456ab78")
        assert acc.status == AccountStatus.VERIFIED
        assert acc.verified_at is not None

    async def test_set_deletion_pending_records_timestamp(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="abc123def456ab78",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await session.commit()

        await repo.set_deletion_pending("abc123def456ab78")
        await session.commit()

        acc = await repo.get("abc123def456ab78")
        assert acc.status == AccountStatus.DELETION_PENDING
        assert acc.deletion_requested_at is not None

    async def test_set_deleted_records_timestamp(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="abc123def456ab78",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await session.commit()

        await repo.set_deleted("abc123def456ab78")
        await session.commit()

        acc = await repo.get("abc123def456ab78")
        assert acc.status == AccountStatus.DELETED
        assert acc.deletion_completed_at is not None


class TestDelete:
    async def test_delete_cascades_verifications(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="abc123def456ab78",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await session.commit()

        await repo.delete("abc123def456ab78")
        await session.commit()

        verifs = (
            await session.execute(
                select(AccountVerification).where(
                    AccountVerification.parent_id == "abc123def456ab78"
                )
            )
        ).scalars().all()
        assert verifs == []
        assert await repo.get("abc123def456ab78") is None


class TestUniqueConstraints:
    async def test_id_number_hash_unique(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="parent_aaa",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await session.commit()

        # Same id_number → collision on the unique index
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await repo.create(
                parent_id="parent_bbb",
                name="李四",
                id_number="110101199003078811",
                phone="13800138001",
            )
            await session.commit()

    async def test_phone_hash_unique(self, session) -> None:
        repo = PostgresAccountRepository(session)
        await repo.create(
            parent_id="parent_aaa",
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await session.commit()

        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await repo.create(
                parent_id="parent_bbb",
                name="李四",
                id_number="110101199003078812",
                phone="13800138000",
            )
            await session.commit()
