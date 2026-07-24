"""Unit tests for niki.auth.sms_service (BE-AUTH-002)."""

from __future__ import annotations

import asyncio

import pytest

from niki.auth.sms_service import (
    InMemorySMSCodeService,
    SMSCodeExpired,
    SMSCodeInvalid,
    SMSTooManyAttempts,
    SMSRateLimited,
    set_sms_service,
)


@pytest.fixture
def sms() -> InMemorySMSCodeService:
    svc = InMemorySMSCodeService(
        ttl_seconds=300,
        max_attempts=5,
        cooldown_seconds=60,
    )
    set_sms_service(svc)
    return svc


pytestmark = pytest.mark.asyncio


class TestSend:
    async def test_first_send_returns_six_digits(self, sms: InMemorySMSCodeService) -> None:
        code = await sms.send("13800138000")
        assert code.isdigit() and len(code) == 6

    async def test_resend_within_window_returns_same_code(
        self, sms: InMemorySMSCodeService
    ) -> None:
        c1 = await sms.send("13800138000")
        # Idempotent: within the TTL window we hand back the same code
        # so a frustrated user clicking "resend" repeatedly does not
        # invalidate their own pending code.
        c2 = await sms.send("13800138000")
        assert c1 == c2

    async def test_cooldown_raises_after_consume(self, sms: InMemorySMSCodeService) -> None:
        # After verify() consumes the code, an immediate next send()
        # with no active code AND inside the cooldown window raises.
        code = await sms.send("13800138000")
        await sms.verify("13800138000", code)  # consume
        with pytest.raises(SMSRateLimited):
            await sms.send("13800138000")


class TestVerify:
    async def test_correct_code_passes(self, sms: InMemorySMSCodeService) -> None:
        code = await sms.send("13800138000")
        assert await sms.verify("13800138000", code) is True

    async def test_wrong_code_raises_invalid(self, sms: InMemorySMSCodeService) -> None:
        await sms.send("13800138000")
        with pytest.raises(SMSCodeInvalid):
            await sms.verify("13800138000", "000000")

    async def test_unknown_phone_raises_invalid(self, sms: InMemorySMSCodeService) -> None:
        with pytest.raises(SMSCodeInvalid):
            await sms.verify("13800138000", "000000")

    async def test_consumed_code_cannot_be_reused(
        self, sms: InMemorySMSCodeService
    ) -> None:
        code = await sms.send("13800138000")
        await sms.verify("13800138000", code)
        with pytest.raises(SMSCodeInvalid):
            await sms.verify("13800138000", code)

    async def test_expired_code_raises_expired(self, sms: InMemorySMSCodeService) -> None:
        # Override TTL on the existing record to simulate expiry.
        await sms.send("13800138000")
        # Manually expire the record by reaching into the dict.
        sms._records["13800138000"].expires_at -= 1000
        with pytest.raises(SMSCodeExpired):
            await sms.verify("13800138000", "any")

    async def test_too_many_attempts_raises(self, sms: InMemorySMSCodeService) -> None:
        await sms.send("13800138000")
        # 5 attempts exhaust the budget; subsequent raises SMSTooManyAttempts.
        for _ in range(5):
            with pytest.raises(SMSCodeInvalid):
                await sms.verify("13800138000", "000000")
        # 6th attempt over the limit
        with pytest.raises(SMSTooManyAttempts):
            await sms.verify("13800138000", "000000")
