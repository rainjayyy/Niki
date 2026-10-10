"""Unit tests for niki.auth.token_blacklist (BE-AUTH-001)."""

from __future__ import annotations

import asyncio
import time

import pytest

from niki.auth.token_blacklist import (
    InMemoryTokenBlacklist,
    set_blacklist,
)


@pytest.fixture
def bl() -> InMemoryTokenBlacklist:
    bl = InMemoryTokenBlacklist()
    set_blacklist(bl)
    yield bl
    set_blacklist(InMemoryTokenBlacklist())  # reset to default


class TestRevoke:
    async def test_revoke_marks_as_revoked(self, bl: InMemoryTokenBlacklist) -> None:
        await bl.revoke("jti-123")
        assert await bl.is_revoked("jti-123") is True

    async def test_other_jti_not_revoked(self, bl: InMemoryTokenBlacklist) -> None:
        await bl.revoke("jti-123")
        assert await bl.is_revoked("jti-other") is False

    async def test_revoked_idempotent(self, bl: InMemoryTokenBlacklist) -> None:
        await bl.revoke("jti-123")
        await bl.revoke("jti-123")
        assert await bl.is_revoked("jti-123") is True


class TestExpiry:
    async def test_expired_revocation_drops(self, bl: InMemoryTokenBlacklist) -> None:
        # Revoke with an explicit past timestamp.
        await bl.revoke("jti-old", expires_at=time.time() - 100)
        assert await bl.is_revoked("jti-old") is False


class TestSyncHook:
    async def test_sync_matches_async(self, bl: InMemoryTokenBlacklist) -> None:
        await bl.revoke("jti-1")
        assert bl.is_revoked_sync("jti-1") is True
        assert await bl.is_revoked("jti-1") is True

    async def test_sync_returns_false_for_unknown(self, bl: InMemoryTokenBlacklist) -> None:
        assert bl.is_revoked_sync("jti-unknown") is False


class TestCleanup:
    async def test_cleanup_drops_expired(self, bl: InMemoryTokenBlacklist) -> None:
        await bl.revoke("jti-keep")
        await bl.revoke("jti-expired", expires_at=time.time() - 10)
        assert len(bl) == 2
        dropped = await bl.cleanup_expired()
        assert dropped == 1
        assert len(bl) == 1
        assert bl.is_revoked_sync("jti-keep") is True
