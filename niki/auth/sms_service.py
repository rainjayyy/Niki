"""SMS verification code service (BE-AUTH-002, mock provider).

The contract:
    1. Caller asks to send a code to a phone (mock = log it).
    2. Caller asks to verify a code against a phone.
    3. Service is idempotent on (phone): repeated `send()` before the
       TTL expires returns the SAME code (no spam).

Two implementations are shipped:
  - `InMemorySMSCodeService` - default; single-process / dev / test.
  - `SMSCodeService`         - abstract base; Redis-backed impl is left
                               as a future ticket (BE-AUTH-005-real).

Security defaults:
  - Code TTL: 300 seconds (5 minutes).
  - Max attempts per phone: 5 (subsequent attempts raise).
  - Rate-limit: 1 send per phone per 60 seconds (configurable).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


class SMSError(Exception):
    """Base class for SMS service failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# Specific error codes so the FastAPI handler can map them to §6.3 style.
class SMSRateLimited(SMSError):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(
            "SMS_RATE_LIMITED",
            f"Resend allowed in {retry_after_seconds:.0f}s",
        )
        self.retry_after_seconds = retry_after_seconds


class SMSCodeInvalid(SMSError):
    def __init__(self) -> None:
        super().__init__("SMS_CODE_INVALID", "Verification code is incorrect")


class SMSCodeExpired(SMSError):
    def __init__(self) -> None:
        super().__init__("SMS_CODE_EXPIRED", "Verification code has expired")


class SMSTooManyAttempts(SMSError):
    def __init__(self) -> None:
        super().__init__(
            "SMS_TOO_MANY_ATTEMPTS",
            "Too many failed attempts; please request a new code",
        )


# ---------------------------------------------------------------------------
# Storage contract
# ---------------------------------------------------------------------------


@dataclass
class _SMSRecord:
    code: str
    expires_at: float
    attempts: int = 0


class SMSCodeService(ABC):
    """Contract for SMS code generation / verification / dispatch."""

    @abstractmethod
    async def send(self, phone: str) -> str:
        """Generate (or reuse) a code for `phone` and mock-send it.

        Returns:
            The 6-digit code. Used in dev mode (echoed in the response);
            production deployments must NOT echo the code in HTTP.

        Raises:
            SMSRateLimited: If a code was sent less than `cooldown_seconds`
                ago for the same phone.
        """

    @abstractmethod
    async def verify(self, phone: str, code: str) -> bool:
        """Return True iff the code matches and is still valid.

        Increments the per-phone attempt counter on every call; raises
        SMSTooManyAttempts after `max_attempts` failed tries.
        """


# ---------------------------------------------------------------------------
# In-memory default implementation
# ---------------------------------------------------------------------------


class InMemorySMSCodeService(SMSCodeService):
    """In-process SMS code storage.

    Single-instance only. For horizontal scaling swap in a Redis-backed
    implementation when this ticket ships (BE-AUTH-005-real).
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_attempts: int = 5,
        cooldown_seconds: int = 60,
    ) -> None:
        self._records: dict[str, _SMSRecord] = {}
        self._last_sent_at: dict[str, float] = {}
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts
        self._cooldown = cooldown_seconds
        self._lock = asyncio.Lock()

    async def send(self, phone: str) -> str:
        """Send / re-send the code. Idempotent within the TTL window.

        If an active code exists we return it (re-clicks on "resend"
        don't reset the user's clock). Only when there's no active
        code AND cooldown has not elapsed do we dispatch a fresh one
        and raise SMSRateLimited.
        """
        now = time.time()
        async with self._lock:
            existing = self._records.get(phone)
            if existing is not None and existing.expires_at > now:
                # Active code on file — return it; reset attempts.
                existing.attempts = 0
                record = existing
            else:
                # No active code: gate new dispatch on cooldown.
                last = self._last_sent_at.get(phone, 0.0)
                if now - last < self._cooldown:
                    raise SMSRateLimited(
                        retry_after_seconds=self._cooldown - (now - last)
                    )
                record = _SMSRecord(
                    code=f"{secrets.randbelow(1_000_000):06d}",
                    expires_at=now + self._ttl,
                    attempts=0,
                )
                self._records[phone] = record
            self._last_sent_at[phone] = now

        # Mock dispatch — production swap replaces this with an Alibaba /
        # Tencent SMS API call. NEVER log the raw code; downstream
        # observability should not be able to read it.
        logger.info(
            "sms.send",
            phone_masked=_mask_phone(phone),
            ttl_seconds=self._ttl,
            attempts=record.attempts,
        )
        return record.code

    async def verify(self, phone: str, code: str) -> bool:
        """Verify a code. Returns True on success, raises on failure."""
        async with self._lock:
            record = self._records.get(phone)
            if record is None:
                raise SMSCodeInvalid()
            if record.expires_at <= time.time():
                # Clean up expired record
                self._records.pop(phone, None)
                raise SMSCodeExpired()
            record.attempts += 1
            if record.attempts > self._max_attempts:
                raise SMSTooManyAttempts()
            if record.code != code:
                raise SMSCodeInvalid()
            # Consume the code so it can't be re-used.
            # NOTE: intentionally keep `_last_sent_at[phone]` so the
            # cooldown window extends across the consume boundary.
            self._records.pop(phone, None)
            return True

    def __len__(self) -> int:
        return len(self._records)


def _mask_phone(phone: str) -> str:
    """Mask a phone number for log output: '138****8000'."""
    if len(phone) < 7:
        return "****"
    return f"{phone[:3]}****{phone[-4:]}"


# Process-wide singleton (overridden in tests via set_sms_service).
_sms: SMSCodeService = InMemorySMSCodeService()


def get_sms_service() -> SMSCodeService:
    """Return the process-wide SMSCodeService."""
    return _sms


def set_sms_service(service: SMSCodeService) -> None:
    """Override the process-wide SMS service (for tests / Redis swap)."""
    global _sms
    _sms = service
