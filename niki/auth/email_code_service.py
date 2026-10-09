"""Email verification code service (BE-AUTH-006).

A lightweight sibling of `niki.auth.sms_service`. The contract:

    1. Caller asks `send(email)` — service generates a 6-digit code
       and mocks send via log line.
    2. Caller asks `verify(email, code)` — confirms and consumes.

`EmailCodeService` is an ABC; the default is `InMemoryEmailCodeService`.
The Redis-backed impl is left as a future ticket (BE-AUTH-005-real).

The difference vs SMS is intentional:

  - Longer TTL (10 minutes) so a parent can context-switch to
    their email client and back without losing the code.
  - No per-email cooldown — email is rate-limited at the API
    layer, not here.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


class EmailCodeError(Exception):
    """Base class for email-code service failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class EmailCodeInvalid(EmailCodeError):
    def __init__(self) -> None:
        super().__init__("EMAIL_CODE_INVALID", "Verification code is incorrect")


class EmailCodeExpired(EmailCodeError):
    def __init__(self) -> None:
        super().__init__("EMAIL_CODE_EXPIRED", "Verification code has expired")


class EmailTooManyAttempts(EmailCodeError):
    def __init__(self) -> None:
        super().__init__(
            "EMAIL_TOO_MANY_ATTEMPTS",
            "Too many failed attempts; please request a new code",
        )


@dataclass
class _EmailRecord:
    code: str
    expires_at: float
    attempts: int = 0


class EmailCodeService(ABC):
    """Contract for email code generation / verification / dispatch."""

    @abstractmethod
    async def send(self, email: str) -> str:
        """Generate / reuse a code for `email` and mock-send it.

        Returns the 6-digit code. Used in dev mode (echoed in the
        response); production deployments must NOT echo the code.
        """

    @abstractmethod
    async def verify(self, email: str, code: str) -> bool:
        """Return True iff the code matches and is still valid."""


class InMemoryEmailCodeService(EmailCodeService):
    """In-process email code storage."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_attempts: int = 10,
    ) -> None:
        self._records: dict[str, _EmailRecord] = {}
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts
        self._lock = asyncio.Lock()

    async def send(self, email: str) -> str:
        now = time.time()
        async with self._lock:
            existing = self._records.get(email)
            if existing is not None and existing.expires_at > now:
                existing.attempts = 0
                record = existing
            else:
                record = _EmailRecord(
                    code=f"{secrets.randbelow(1_000_000):06d}",
                    expires_at=now + self._ttl,
                    attempts=0,
                )
                self._records[email] = record

        logger.info(
            "email_code.send",
            email_masked=_mask_email(email),
            ttl_seconds=self._ttl,
        )
        return record.code

    async def verify(self, email: str, code: str) -> bool:
        async with self._lock:
            record = self._records.get(email)
            if record is None:
                raise EmailCodeInvalid()
            if record.expires_at <= time.time():
                self._records.pop(email, None)
                raise EmailCodeExpired()
            record.attempts += 1
            if record.attempts > self._max_attempts:
                raise EmailTooManyAttempts()
            if record.code != code:
                raise EmailCodeInvalid()
            self._records.pop(email, None)
            return True

    def __len__(self) -> int:
        return len(self._records)


def _mask_email(email: str) -> str:
    """Mask an email for log output: 'a****@example.com'."""
    if "@" not in email:
        return "****"
    local, domain = email.split("@", 1)
    if not local:
        return f"****@{domain}"
    if len(local) <= 1:
        return f"{local}****@{domain}"
    return f"{local[0]}****@{domain}"


# Process-wide singleton
_email: EmailCodeService = InMemoryEmailCodeService()


def get_email_code_service() -> EmailCodeService:
    """Return the process-wide EmailCodeService."""
    return _email


def set_email_code_service(service: EmailCodeService) -> None:
    """Override the process-wide service (tests / Redis swap)."""
    global _email
    _email = service
