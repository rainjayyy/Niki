"""Integration test: AccountLifecycle on top of PostgresAccountRepository.

Mirror of tests/auth/test_lifecycle.py but exercising the Postgres
repository rather than the in-memory fallback.
"""

from __future__ import annotations

import pytest

from niki.auth.account import AccountStatus, AuthError
from niki.auth.lifecycle import AccountLifecycle
from niki.db.repositories import PostgresAccountRepository


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def lifecycle(session):
    """AccountLifecycle wired to a fresh per-test session."""
    repo = PostgresAccountRepository(session)
    lc = AccountLifecycle(repo=repo)
    yield lc
    await session.commit()


class TestSignupDBSuccess:
    async def test_full_signup_flow(self, lifecycle: AccountLifecycle) -> None:
        account, _ = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await lifecycle.verify_real_name(
            parent_id=account.id,
            name="张三",
            id_number="110101199003078811",
        )
        await lifecycle.verify_face(
            parent_id=account.id,
            live_image_url="https://example.com/face.jpg",
        )
        await lifecycle.sign_consent(
            parent_id=account.id,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )

        updated = await lifecycle.get_account(account.id)
        assert updated.status == AccountStatus.VERIFIED
        assert updated.verified_at is not None

    async def test_get_progress_for_real_account(self, lifecycle: AccountLifecycle) -> None:
        account, progress = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        assert progress is not None
        assert len(progress.steps) == 3


class TestSignupDBErrors:
    async def test_invalid_id_raises(self, lifecycle: AccountLifecycle) -> None:
        with pytest.raises(AuthError) as exc_info:
            await lifecycle.start_signup(
                name="张三",
                id_number="not-an-id",
                phone="13800138000",
            )
        assert exc_info.value.code == "INVALID_ID_FORMAT"

    async def test_real_name_failure_marks_step(self, lifecycle: AccountLifecycle) -> None:
        account, _ = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        with pytest.raises(AuthError):
            await lifecycle.verify_real_name(
                parent_id=account.id,
                name="张三",
                id_number="bad",  # invalid format
            )
        progress = await lifecycle.get_progress(account.id)
        rn_step = next(s for s in progress.steps if s.name == "real_name")
        assert rn_step.completed is False
        assert rn_step.error is not None


class TestDeletionDB:
    async def test_request_then_complete(self, lifecycle: AccountLifecycle) -> None:
        account, _ = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        deleted = await lifecycle.request_deletion(account.id)
        assert deleted.status == AccountStatus.DELETION_PENDING

        final = await lifecycle.complete_deletion(account.id)
        assert final.status == AccountStatus.DELETED

        # Row is hard-deleted
        assert await lifecycle.get_account(account.id) is None

    async def test_double_request_raises(self, lifecycle: AccountLifecycle) -> None:
        account, _ = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await lifecycle.request_deletion(account.id)
        with pytest.raises(AuthError) as exc_info:
            await lifecycle.request_deletion(account.id)
        assert exc_info.value.code == "ALREADY_PENDING"

    async def test_complete_without_request_raises(
        self, lifecycle: AccountLifecycle
    ) -> None:
        account, _ = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        with pytest.raises(AuthError) as exc_info:
            await lifecycle.complete_deletion(account.id)
        assert exc_info.value.code == "DELETION_NOT_PENDING"
