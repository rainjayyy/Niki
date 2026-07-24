"""Token blacklist for logout enforcement (BE-AUTH-001).

The blacklist tracks `jti` strings of revoked tokens so the auth
dependency can short-circuit expired-but-revoked tokens.

Two implementations:
  - `InMemoryTokenBlacklist`  - default; thread-safe via asyncio.Lock.
  - `TokenBlacklist`          - abstract base (Redis-backed contract
                                is documented but not implemented yet).

Operations are O(1) on the in-memory backend.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod


class TokenBlacklist(ABC):
    """Contract for token blacklist storage."""

    @abstractmethod
    async def revoke(self, jti: str) -> None:
        """Mark a token's jti as revoked (effective immediately)."""

    @abstractmethod
    async def is_revoked(self, jti: str) -> bool:
        """Return True if `jti` has been revoked and not yet expired."""

    @abstractmethod
    def is_revoked_sync(self, jti: str) -> bool:
        """Synchronous revocation check used by jwt_service.verify_token.

        Both async and sync flavors MUST agree on answers for correctness.
        Production deployments using a remote backend (Redis) should
        cache the answer locally to make this cheap.
        """

    @abstractmethod
    async def cleanup_expired(self) -> int:
        """Drop expired entries; returns the count dropped (telemetry)."""


class InMemoryTokenBlacklist(TokenBlacklist):
    """In-process token blacklist — single-instance deployments only.

    Honours per-entry expiry so the dict doesn't grow unboundedly.
    """

    def __init__(self) -> None:
        self._store: dict[str, float] = {}  # jti -> expires_at (unix-ts)
        self._lock = asyncio.Lock()

    async def revoke(self, jti: str, *, expires_at: float | None = None) -> None:
        if expires_at is None:
            expires_at = time.time() + 30 * 24 * 3600
        async with self._lock:
            self._store[jti] = expires_at

    async def is_revoked(self, jti: str) -> bool:
        exp = self._store.get(jti)
        if exp is None:
            return False
        if exp <= time.time():
            async with self._lock:
                self._store.pop(jti, None)
            return False
        return True

    def is_revoked_sync(self, jti: str) -> bool:
        """Sync check — needed for jwt_service.verify_token's sync hook."""
        exp = self._store.get(jti)
        if exp is None:
            return False
        if exp <= time.time():
            return False  # lazy cleanup happens on async path
        return True

    async def cleanup_expired(self) -> int:
        now = time.time()
        async with self._lock:
            before = len(self._store)
            self._store = {j: e for j, e in self._store.items() if e > now}
            return before - len(self._store)

    def __len__(self) -> int:
        return len(self._store)


# Module-level singleton — production wiring can swap this for Redis.
_blacklist: TokenBlacklist = InMemoryTokenBlacklist()


def get_blacklist() -> TokenBlacklist:
    """Return the process-wide token blacklist (in-memory by default)."""
    return _blacklist


def set_blacklist(blacklist: TokenBlacklist) -> None:
    """Override the process-wide blacklist (for tests / Redis migration)."""
    global _blacklist
    _blacklist = blacklist
