"""Tests for account lifecycle (signup + deletion)."""

import pytest

from niki.auth import (
    AccountStatus,
    AuthError,
)
from niki.auth.lifecycle import AccountLifecycle


@pytest.fixture
def lifecycle() -> AccountLifecycle:
    return AccountLifecycle()


@pytest.fixture
def started_account(lifecycle: AccountLifecycle):
    return lifecycle.start_signup(
        name="张三",
        id_number="110101199003078811",
        phone="13800138000",
    )


class TestSignupStart:
    async def test_starts_in_pending(self, lifecycle: AccountLifecycle) -> None:
        account, progress = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        assert account.status == AccountStatus.PENDING
        assert account.is_verified is False
        assert account.id_number_masked == "110101****8811"
        assert account.phone_masked == "138****8000"

    async def test_progress_has_3_steps(self, lifecycle: AccountLifecycle) -> None:
        _, progress = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        step_names = [s.name for s in progress.steps]
        assert step_names == ["real_name", "face", "consent"]

    async def test_invalid_id_raises(self, lifecycle: AccountLifecycle) -> None:
        with pytest.raises(AuthError) as exc_info:
            await lifecycle.start_signup(
                name="张三",
                id_number="123",
                phone="13800138000",
            )
        assert exc_info.value.code == "INVALID_ID_FORMAT"

    async def test_invalid_phone_raises(self, lifecycle: AccountLifecycle) -> None:
        with pytest.raises(AuthError) as exc_info:
            await lifecycle.start_signup(
                name="张三",
                id_number="110101199003078811",
                phone="abc",
            )
        assert exc_info.value.code == "INVALID_PHONE_FORMAT"


class TestFullSignupFlow:
    async def test_all_steps_completed_marks_verified(self, lifecycle: AccountLifecycle) -> None:
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
        # Re-fetch to get updated status
        updated = lifecycle.get_account(account.id)
        assert updated.status == AccountStatus.VERIFIED
        assert updated.is_verified is True
        assert updated.verified_at is not None

    async def test_real_name_failure_blocks_progress(self, lifecycle: AccountLifecycle) -> None:
        account, progress = await lifecycle.start_signup(
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
        # Step should be marked failed
        real_name_step = next(s for s in progress.steps if s.name == "real_name")
        assert real_name_step.completed is False
        assert real_name_step.error is not None

    async def test_face_failure_blocks_progress(self, lifecycle: AccountLifecycle) -> None:
        account, progress = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await lifecycle.verify_real_name(
            parent_id=account.id,
            name="张三",
            id_number="110101199003078811",
        )
        with pytest.raises(AuthError):
            await lifecycle.verify_face(
                parent_id=account.id,
                live_image_url="not-a-url",
            )
        face_step = next(s for s in progress.steps if s.name == "face")
        assert face_step.completed is False


class TestDeletionFlow:
    async def test_request_deletion_marks_pending(self, lifecycle: AccountLifecycle) -> None:
        account, _ = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        updated = await lifecycle.request_deletion(account.id)
        assert updated.status == AccountStatus.DELETION_PENDING
        assert updated.deletion_requested_at is not None

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

    async def test_complete_deletion_erases_account(self, lifecycle: AccountLifecycle) -> None:
        account, _ = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await lifecycle.request_deletion(account.id)
        deleted = await lifecycle.complete_deletion(account.id)
        assert deleted.status == AccountStatus.DELETED
        assert deleted.deletion_completed_at is not None
        # Account should no longer be in storage
        assert lifecycle.get_account(account.id) is None

    async def test_complete_deletion_without_request_raises(
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

    async def test_complete_deletion_on_unknown_account_raises(
        self, lifecycle: AccountLifecycle
    ) -> None:
        with pytest.raises(AuthError):
            await lifecycle.request_deletion("nonexistent-id")


class TestAccountLookup:
    async def test_get_account(self, lifecycle: AccountLifecycle) -> None:
        account, _ = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        assert lifecycle.get_account(account.id) is account

    async def test_get_nonexistent(self, lifecycle: AccountLifecycle) -> None:
        assert lifecycle.get_account("nonexistent") is None

    async def test_list_accounts(self, lifecycle: AccountLifecycle) -> None:
        await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await lifecycle.start_signup(
            name="李四",
            id_number="110101199003078812",
            phone="13800138001",
        )
        accounts = lifecycle.list_accounts()
        assert len(accounts) == 2

    def test_progress_for_unknown_account(self, lifecycle: AccountLifecycle) -> None:
        assert lifecycle.get_progress("nonexistent") is None
