"""Tests for parent account model + validation."""


from niki.auth.account import (
    AccountStatus,
    AuthError,
    ParentAccount,
    mask_id_number,
    mask_phone,
    validate_id_number,
    validate_phone,
)


class TestValidation:
    """Input format validation."""

    def test_valid_id_number(self) -> None:
        assert validate_id_number("110101199003078811")
        assert validate_id_number("11010119900307881X")

    def test_invalid_id_number(self) -> None:
        assert not validate_id_number("123")
        assert not validate_id_number("11010119900307881")  # 17 digits
        assert not validate_id_number("1101011990030788112")  # 19 digits

    def test_valid_phone(self) -> None:
        assert validate_phone("13800138000")
        assert validate_phone("+8613800138000")
        assert validate_phone("138-0013-8000") is False  # hyphens not allowed

    def test_invalid_phone(self) -> None:
        assert not validate_phone("123")
        assert not validate_phone("")


class TestMasking:
    """Sensitive data masking for logs."""

    def test_mask_id(self) -> None:
        assert mask_id_number("110101199003078811") == "110101****8811"

    def test_mask_short_id(self) -> None:
        assert mask_id_number("123") == "****"

    def test_mask_phone(self) -> None:
        assert mask_phone("13800138000") == "138****8000"

    def test_mask_short_phone(self) -> None:
        assert mask_phone("123") == "****"


class TestParentAccount:
    """Parent account model."""

    def test_create_account(self) -> None:
        acc = ParentAccount(
            id="p-001",
            name="张三",
            id_number_masked="110101****8811",
            phone_masked="138****8000",
        )
        assert acc.id == "p-001"
        assert acc.status == AccountStatus.PENDING
        assert acc.is_verified is False
        assert acc.child_ids == []

    def test_verified_status(self) -> None:
        acc = ParentAccount(
            id="p-001",
            name="张三",
            id_number_masked="110101****8811",
            phone_masked="138****8000",
            status=AccountStatus.VERIFIED,
        )
        assert acc.is_verified is True

    def test_to_dict_masks_sensitive_data(self) -> None:
        acc = ParentAccount(
            id="p-001",
            name="张三",
            id_number_masked="110101****8811",
            phone_masked="138****8000",
        )
        d = acc.to_dict()
        # Should never contain raw ID
        assert "110101199003078811" not in str(d)
        # Should contain masked version
        assert "110101****8811" in str(d)

    def test_deletion_window_remaining(self) -> None:
        from datetime import datetime, timedelta

        acc = ParentAccount(
            id="p-001",
            name="张三",
            id_number_masked="110101****8811",
            phone_masked="138****8000",
            status=AccountStatus.DELETION_PENDING,
            deletion_requested_at=datetime.utcnow() - timedelta(days=5),
        )
        # 30 - 5 = 25 days remaining
        assert acc.deletion_window_remaining_days == 25

    def test_deletion_window_expired(self) -> None:
        from datetime import datetime, timedelta

        acc = ParentAccount(
            id="p-001",
            name="张三",
            id_number_masked="110101****8811",
            phone_masked="138****8000",
            status=AccountStatus.DELETION_PENDING,
            deletion_requested_at=datetime.utcnow() - timedelta(days=35),
        )
        # Past 30 days
        assert acc.deletion_window_remaining_days == 0

    def test_deletion_window_not_set(self) -> None:
        acc = ParentAccount(
            id="p-001",
            name="张三",
            id_number_masked="110101****8811",
            phone_masked="138****8000",
        )
        # Not in deletion pending
        assert acc.deletion_window_remaining_days is None


class TestAuthError:
    """Auth error structure."""

    def test_error_has_code_and_message(self) -> None:
        exc = AuthError("INVALID_ID", "ID is invalid")
        assert exc.code == "INVALID_ID"
        assert exc.message == "ID is invalid"
        assert "ID is invalid" in str(exc)
