"""Parent ↔ Child pairing code store (BE-DEV-004).

The frontend parent app needs a way to bind a child device to a
parent account. The flow is:

1. Parent opens "Bind a device" screen → backend mints a 6-digit
   code tied to the parent's `child_id` and returns it.
2. The kid's H5 prompts for the code; the kid enters it.
3. The kid's H5 calls `consume(code)` with the 6-digit string;
   backend marks the code as used and returns the parent's id
   (so the kid's H5 now knows which parent it represents).
4. A confirmation push goes to the parent's existing devices
   (handled downstream — see `BE-PUSH-002`).

Properties enforced here:

- **6-digit numeric code** (000000–999999, zero-padded on serialise)
- **5-minute TTL** from creation
- **Single-use**: `consumed_at` is set on first consume; second
  consume returns `None`.
- **Collision-safe**: code generation retries until it finds a
  free 6-digit slot (capped at 1000 attempts to surface bugs).
- **Thread-safe**: dict + `threading.Lock` (same pattern as
  `InMemorySessionStore`).

The default implementation is `InMemoryPairingCodeStore`. Future
PRs can swap in a Redis-backed store by following the
`PairingCodeStore` ABC; callers should always go through the
process-wide singleton via `get_pairing_store()` /
`set_pairing_store()`.
"""

from __future__ import annotations

import random
import secrets
import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 5-minute TTL per frontend-integration §2.2.
PAIRING_CODE_TTL_SECONDS: int = 5 * 60

# Numeric range for 6-digit codes.
_CODE_MIN: int = 0
_CODE_MAX: int = 999_999
_CODE_DIGITS: int = 6

# Hard ceiling on collision retries. With at most 1000 codes in
# flight the birthday-bound collision probability is < 0.001%;
# anything beyond that is almost certainly a bug.
_MAX_GENERATION_ATTEMPTS: int = 1_000


def _utcnow() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class PairingCode:
    """A single-use 6-digit code binding a child to a parent."""

    code: str
    parent_id: str
    child_id: str
    created_at: datetime
    expires_at: datetime
    consumed_at: Optional[datetime] = None

    @classmethod
    def mint(
        cls,
        *,
        parent_id: str,
        child_id: str,
        ttl_seconds: int = PAIRING_CODE_TTL_SECONDS,
        code: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> "PairingCode":
        """Build a fresh, un-consumed PairingCode.

        Args:
            parent_id: Parent account id (FK).
            child_id:  Child device id (FK).
            ttl_seconds: Validity window. Defaults to 5 minutes.
            code: Pre-generated 6-digit string (used by the store's
                  collision-loop). Caller MUST validate it's already
                  in range. Defaults to a fresh random code.
            now: Override "now" (used by tests to simulate expiry).
                 Defaults to `_utcnow()`.
        """
        ts = now or _utcnow()
        expires = ts + timedelta(seconds=ttl_seconds)
        if code is None:
            code = _format_code(secrets.randbelow(_CODE_MAX - _CODE_MIN + 1))
        return cls(
            code=code,
            parent_id=parent_id,
            child_id=child_id,
            created_at=ts,
            expires_at=expires,
            consumed_at=None,
        )

    @property
    def is_expired(self) -> bool:
        return _utcnow() >= self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def to_dict(self) -> dict:
        """Serialise for API responses (no secrets beyond the code)."""
        return {
            "code": self.code,
            "parent_id": self.parent_id,
            "child_id": self.child_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "consumed_at": (self.consumed_at.isoformat() if self.consumed_at is not None else None),
        }


def _format_code(n: int) -> str:
    """Format an integer in [0, 999999] as a zero-padded 6-digit string."""
    if not (0 <= n <= 999_999):
        raise ValueError(f"code out of range: {n!r}")
    return f"{n:06d}"


def _validate_code(code: str) -> str:
    """Validate the shape of an incoming 6-digit code string."""
    if not isinstance(code, str):
        raise ValueError("code must be a string")
    if len(code) != _CODE_DIGITS or not code.isdigit():
        raise ValueError(f"code must be {_CODE_DIGITS} digits (got {code!r})")
    return code


# ---------------------------------------------------------------------------
# Store ABC
# ---------------------------------------------------------------------------


class PairingCodeStore(ABC):
    """Contract for pairing-code persistence."""

    @abstractmethod
    def create(self, parent_id: str, child_id: str) -> PairingCode:
        """Mint a new code for the given (parent, child).

        Returns the freshly-created `PairingCode`. Implementations
        MUST guarantee uniqueness within their own view of the
        world (re-roll on collision).
        """

    @abstractmethod
    def consume(self, code: str) -> Optional[PairingCode]:
        """Atomically consume a code.

        Returns the `PairingCode` on success, or `None` if the code
        is unknown, expired, or already consumed. Implementations
        MUST ensure that two concurrent consume() calls cannot
        both succeed.
        """

    @abstractmethod
    def cleanup_expired(self) -> int:
        """Drop expired entries. Returns the count removed."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryPairingCodeStore(PairingCodeStore):
    """Thread-safe dict-backed pairing code store.

    Codes are keyed by the 6-digit string. Single-use and TTL
    enforcement both happen under the same lock so concurrent
    consume() calls cannot double-spend a code.
    """

    def __init__(self) -> None:
        self._codes: dict[str, PairingCode] = {}
        self._lock = threading.Lock()
        self._rng = random.SystemRandom()

    # ----- public API -----

    def create(self, parent_id: str, child_id: str) -> PairingCode:
        """Mint a fresh code, retrying on collision.

        Birthday paradox: with 1000 active codes the per-code
        collision probability is ~0.001%, so 1000 attempts is
        more than enough headroom. If we somehow fail to find a
        free code after that many tries, raise `RuntimeError` —
        it's a clear signal that something is wrong upstream
        (e.g. an unbounded burst of `create()` calls).
        """
        with self._lock:
            for _ in range(_MAX_GENERATION_ATTEMPTS):
                candidate = _format_code(self._rng.randint(_CODE_MIN, _CODE_MAX))
                if candidate not in self._codes:
                    record = PairingCode.mint(
                        parent_id=parent_id,
                        child_id=child_id,
                        code=candidate,
                    )
                    self._codes[candidate] = record
                    return record
            logger.error(
                "pairing.code_generation_exhausted",
                active=len(self._codes),
                attempts=_MAX_GENERATION_ATTEMPTS,
            )
            raise RuntimeError(
                "Unable to generate a unique 6-digit pairing code "
                f"after {_MAX_GENERATION_ATTEMPTS} attempts "
                f"(active codes = {len(self._codes)})"
            )

    def consume(self, code: str) -> Optional[PairingCode]:
        """Atomically consume a code under the lock.

        Returns:
            The `PairingCode` if it exists, is unexpired, and has
            not yet been consumed. Otherwise `None`.

        The returned object is a *deep copy* so callers can mutate
            it without poisoning internal state. The store keeps
            the original (now-consumed) record.
        """
        # Defensive: accept either already-validated or raw strings.
        try:
            normalised = _validate_code(code)
        except ValueError:
            return None
        with self._lock:
            record = self._codes.get(normalised)
            if record is None:
                return None
            if record.is_consumed:
                return None
            if record.is_expired:
                return None
            record.consumed_at = _utcnow()
            return deepcopy(record)

    def cleanup_expired(self) -> int:
        """Remove expired entries (whether consumed or not)."""
        now = _utcnow()
        with self._lock:
            before = len(self._codes)
            self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}
            return before - len(self._codes)

    # ----- introspection (tests / debugging) -----

    def __len__(self) -> int:
        return len(self._codes)

    def peek(self, code: str) -> Optional[PairingCode]:
        """Read-only lookup that doesn't mutate consumed_at."""
        try:
            normalised = _validate_code(code)
        except ValueError:
            return None
        with self._lock:
            record = self._codes.get(normalised)
            return deepcopy(record) if record is not None else None


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_store: Optional[PairingCodeStore] = None


def get_pairing_store() -> PairingCodeStore:
    """Return the process-wide pairing code store (in-memory by default)."""
    global _store
    if _store is None:
        _store = InMemoryPairingCodeStore()
    return _store


def set_pairing_store(store: Optional[PairingCodeStore]) -> None:
    """Override the process-wide store (for tests / future Redis impl)."""
    global _store
    _store = store
