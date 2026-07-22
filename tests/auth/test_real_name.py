"""Tests for real-name verification."""

import pytest

from niki.auth.account import AuthError
from niki.auth.real_name import RealNameVerifier


@pytest.fixture
def verifier() -> RealNameVerifier:
    return RealNameVerifier()


class TestRealNameVerification:
    async def test_valid_name_and_id(self, verifier: RealNameVerifier) -> None:
        result = await verifier.verify("张三", "110101199003078811")
        assert result.verified
        assert result.confidence > 0.8
        assert result.id_number_masked == "110101****8811"
        assert result.error is None
        assert result.verified_at is not None

    async def test_invalid_id_format_raises(self, verifier: RealNameVerifier) -> None:
        with pytest.raises(AuthError) as exc_info:
            await verifier.verify("张三", "123")
        assert exc_info.value.code == "INVALID_ID_FORMAT"

    async def test_empty_name_raises(self, verifier: RealNameVerifier) -> None:
        with pytest.raises(AuthError) as exc_info:
            await verifier.verify("", "110101199003078811")
        assert exc_info.value.code == "INVALID_NAME_FORMAT"

    async def test_result_to_dict(self, verifier: RealNameVerifier) -> None:
        result = await verifier.verify("李四", "110101199003078811")
        d = result.to_dict()
        assert d["verified"] is True
        assert d["name"] == "李四"
        assert "110101****8811" in d["id_number_masked"]
        assert d["provider"] == "stub-id-verifier"

    async def test_mask_in_result(self, verifier: RealNameVerifier) -> None:
        result = await verifier.verify("王五", "110101199003078811")
        # The raw ID must never appear in the result
        assert "110101199003078811" not in str(result.to_dict())
