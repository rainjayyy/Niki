"""Captcha / Turnstile verification layer (BE-COMP-006).

The contract:
    1. Frontend hits `/auth/captcha/challenge` to mint a challenge
       (returns id + question + expires_at).
    2. User solves it; frontend POSTs to `/auth/captcha/verify`
       with `(captcha_id, answer)` and gets `{valid: bool}`.
    3. Optionally, the same `(captcha_id, answer)` is included
       in `/auth/sms/send`. When `Settings.sms_captcha_required`
       is True, missing / invalid captcha → 400 CAPTCHA_INVALID.

The v1 backend is a *pluggable* `CaptchaService`:
    - `InMemoryCaptchaService` (default; arithmetic puzzle).

Swapping in 阿里云 / 网易 / Cloudflare Turnstile is a followup
ticket (BE-COMP-006-real) and does NOT change the API surface
— just drop in a different `CaptchaService` impl via
`set_captcha_service(...)`.

Security:
    - The plaintext answer is **never** persisted. We only store
      `sha256(answer)` so a memory dump / log leak cannot hand
      out reusable answers.
    - TTL = 5 min (configurable).
    - Cap = 1000 entries; on overflow the oldest is dropped.
    - Single-use: a successful verify() consumes the challenge.
    - Process-local; multi-worker needs Redis pub/sub
      (BE-COMP-006-real).
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CaptchaError(Exception):
    """Base class for captcha service failures."""


class CaptchaInvalid(CaptchaError):
    """Raised when the presented challenge_id is unknown / expired."""

    def __init__(self) -> None:
        super().__init__("CAPTCHA_INVALID", "Captcha challenge is missing or expired")
        self.code = "CAPTCHA_INVALID"
        self.message = "Captcha challenge is missing or expired"


# ---------------------------------------------------------------------------
# Storage record + ABC
# ---------------------------------------------------------------------------


@dataclass
class CaptchaChallenge:
    """Public representation of a freshly-minted captcha challenge."""

    id: str  # hex uuid4 — opaque to clients
    question: str  # e.g. "3 + 4 = ?"
    answer_hash: str  # sha256(answer) — never the plaintext
    expires_at: float  # time.time() seconds


class _StoredCaptcha:
    """Internal record kept in the dict; not exposed."""

    __slots__ = ("id", "answer_hash", "expires_at")

    def __init__(self, id: str, answer_hash: str, expires_at: float) -> None:
        self.id = id
        self.answer_hash = answer_hash
        self.expires_at = expires_at


class CaptchaService(ABC):
    """Contract for captcha challenge creation + verification.

    The interface is intentionally minimal so a real Turnstile
    backend can drop in (see BE-COMP-006-real) without changing
    the FastAPI handlers.
    """

    @abstractmethod
    async def create_challenge(self) -> CaptchaChallenge:
        """Mint a fresh challenge. Caller returns the public
        `CaptchaChallenge` (id, question, expires_at) to the client.
        """

    @abstractmethod
    async def verify(self, challenge_id: str, answer: str | int) -> bool:
        """Return True iff the challenge exists, is unexpired, and
        `sha256(answer)` matches. A successful verify consumes the
        challenge so it cannot be re-used.
        """


# ---------------------------------------------------------------------------
# In-memory default implementation (arithmetic puzzle)
# ---------------------------------------------------------------------------


# Public so tests can construct the same answers deterministically.
_OPS: tuple[str, ...] = ("+", "-", "*")


def _hash_answer(answer: str | int) -> str:
    """SHA-256 of the canonical string form of the answer.

    Stored once at challenge creation; compared on every verify.
    """
    return hashlib.sha256(str(answer).encode("utf-8")).hexdigest()


def _generate_question_and_answer() -> tuple[str, int]:
    """Build an arithmetic puzzle whose answer is a small integer.

    Range bounded to keep puzzles visually scannable for human
    users (and to make brute-force over the 17×17×3 = 867-element
    answer space wasteful without solving the math).
    """
    op = random.choice(_OPS)
    if op == "+":
        a = random.randint(1, 20)
        b = random.randint(1, 20)
        answer = a + b
    elif op == "-":
        a = random.randint(5, 20)
        b = random.randint(1, a - 1)  # keep non-negative
        answer = a - b
    else:  # "*"
        a = random.randint(1, 9)
        b = random.randint(1, 9)
        answer = a * b
    return f"{a} {op} {b} = ?", answer


class InMemoryCaptchaService(CaptchaService):
    """In-process captcha store. Default for dev / test / single-worker.

    Bounded LRU-ish: when `cap` entries are on file, the oldest is
    dropped to make room for a new challenge. The cap is generous
    enough that real-world traffic will never bump against it; the
    intent is to prevent a memory-leak DoS via repeated GETs of
    `/auth/captcha/challenge`.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,  # 5 min — matches SMS code TTL
        cap: int = 1000,
    ) -> None:
        self._records: dict[str, _StoredCaptcha] = {}
        # Insertion order doubles as eviction order (oldest first).
        self._ttl = ttl_seconds
        self._cap = cap
        self._lock = asyncio.Lock()

    # --- helpers ---------------------------------------------------------

    def _sweep_expired(self, now: float) -> None:
        """Drop expired entries. Caller must hold the lock."""
        expired = [k for k, v in self._records.items() if v.expires_at <= now]
        for k in expired:
            self._records.pop(k, None)

    def _evict_oldest_if_full(self) -> None:
        """Drop the oldest entry (insertion-order first) when at cap.
        Caller must hold the lock."""
        while len(self._records) >= self._cap:
            oldest = next(iter(self._records))
            self._records.pop(oldest, None)

    # --- public API ------------------------------------------------------

    async def create_challenge(self) -> CaptchaChallenge:
        """Mint a fresh arithmetic captcha challenge."""
        async with self._lock:
            self._sweep_expired(time.time())
            self._evict_oldest_if_full()
            question, answer = _generate_question_and_answer()
            challenge_id = secrets.token_hex(16)
            now = time.time()
            stored = _StoredCaptcha(
                id=challenge_id,
                answer_hash=_hash_answer(answer),
                expires_at=now + self._ttl,
            )
            self._records[challenge_id] = stored
        return CaptchaChallenge(
            id=challenge_id,
            question=question,
            answer_hash=stored.answer_hash,
            expires_at=stored.expires_at,
        )

    async def verify(self, challenge_id: str, answer: str | int) -> bool:
        """Return True iff the challenge is known, unexpired, and the
        hashed answer matches. Consumes the challenge on success so
        a replay of the same `(id, answer)` returns False the second
        time.
        """
        if not challenge_id or answer is None:
            return False
        async with self._lock:
            record = self._records.pop(challenge_id, None)
            if record is None:
                return False
            if record.expires_at <= time.time():
                # Already expired; do NOT re-store.
                return False
            return _hash_answer(answer) == record.answer_hash

    def __len__(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# Singleton accessors (mirrors niki.auth.sms_service)
# ---------------------------------------------------------------------------


_captcha: CaptchaService = InMemoryCaptchaService()


def get_captcha_service() -> CaptchaService:
    """Return the process-wide CaptchaService."""
    return _captcha


def set_captcha_service(service: CaptchaService) -> None:
    """Override the process-wide captcha service (tests / real plug-in)."""
    global _captcha
    _captcha = service
