"""Tests for the SessionStore abstraction (BE-SESSION-redis)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from niki.session.limiter import ExitReason
from niki.session.store import (
    FallbackSessionStore,
    InMemorySessionStore,
    SessionStore,
    SyncSessionStore,
    _iso,
    _parse_iso,
)


# ---------------------------------------------------------------------------
# ISO helpers
# ---------------------------------------------------------------------------


class TestIsoHelpers:
    def test_iso_roundtrip_aware(self) -> None:
        dt = datetime(2026, 7, 22, 14, 30, tzinfo=timezone.utc)
        s = _iso(dt)
        assert s.endswith("Z")
        parsed = _parse_iso(s)
        assert parsed == dt

    def test_iso_roundtrip_naive_treats_as_utc(self) -> None:
        dt = datetime(2026, 7, 22, 14, 30)  # naive
        s = _iso(dt)
        parsed = _parse_iso(s)
        assert parsed == dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# InMemorySessionStore
# ---------------------------------------------------------------------------


class TestInMemorySessionStore:
    def test_get_unknown_returns_none(self) -> None:
        store = InMemorySessionStore()
        assert store.get_started_at("c-1") is None
        assert store.get_last_activity("c-1") is None
        assert store.is_soft_warned("c-1") is False
        assert store.get_ended("c-1") is None

    def test_set_get_roundtrip(self) -> None:
        store = InMemorySessionStore()
        ts = datetime.now(timezone.utc)
        store.set_started_at("c-1", ts)
        assert store.get_started_at("c-1") == ts

    def test_set_started_at_normalises_naive_to_utc(self) -> None:
        store = InMemorySessionStore()
        naive = datetime(2026, 7, 22, 14, 30)
        store.set_started_at("c-1", naive)
        stored = store.get_started_at("c-1")
        assert stored.tzinfo is not None
        assert stored == naive.replace(tzinfo=timezone.utc)

    def test_clear_started_at(self) -> None:
        store = InMemorySessionStore()
        store.set_started_at("c-1", datetime.now(timezone.utc))
        store.clear_started_at("c-1")
        assert store.get_started_at("c-1") is None

    def test_soft_warned_flag(self) -> None:
        store = InMemorySessionStore()
        assert not store.is_soft_warned("c-1")
        store.mark_soft_warned("c-1")
        assert store.is_soft_warned("c-1")
        store.clear_soft_warned("c-1")
        assert not store.is_soft_warned("c-1")

    def test_ended_roundtrip(self) -> None:
        store = InMemorySessionStore()
        store.set_ended("c-1", "hard_limit")
        assert store.get_ended("c-1") == "hard_limit"
        store.clear_ended("c-1")
        assert store.get_ended("c-1") is None

    def test_independent_children(self) -> None:
        store = InMemorySessionStore()
        ts1 = datetime(2026, 7, 22, tzinfo=timezone.utc)
        ts2 = datetime(2026, 7, 23, tzinfo=timezone.utc)
        store.set_started_at("c-1", ts1)
        store.set_started_at("c-2", ts2)
        assert store.get_started_at("c-1") == ts1
        assert store.get_started_at("c-2") == ts2
        store.clear_started_at("c-1")
        assert store.get_started_at("c-2") == ts2  # c-2 unaffected

    def test_returned_datetime_is_copy(self) -> None:
        """Mutating the returned value must not affect the store."""
        store = InMemorySessionStore()
        ts = datetime.now(timezone.utc)
        store.set_started_at("c-1", ts)
        got = store.get_started_at("c-1")
        # Try to add a day to the returned value (this would only
        # work if it's a mutable datetime; in Python datetime is
        # immutable, but the test documents intent).
        assert got == ts


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


class TestABCConformance:
    def test_in_memory_satisfies_abc(self) -> None:
        assert isinstance(InMemorySessionStore(), SessionStore)


# ---------------------------------------------------------------------------
# FallbackSessionStore
# ---------------------------------------------------------------------------


class TestFallbackSessionStore:
    async def test_falls_back_on_primary_failure(self) -> None:
        # Build a primary that raises on get_started_at. Use a
        # plain AsyncMock (no spec) so its attributes are
        # automatically AsyncMock instances.
        primary = AsyncMock()
        primary.get_started_at.side_effect = ConnectionError("redis down")
        fallback = InMemorySessionStore()
        store = FallbackSessionStore(primary, fallback)

        # Pre-condition: fallback not yet engaged
        assert not store.in_fallback

        # Drive a call that should fail
        result = await store.get_started_at("c-1")

        # We got None (the fallback returned it for an unknown
        # child) and the primary was called, but it raised —
        # so we should now be in fallback mode.
        assert store.in_fallback
        primary.get_started_at.assert_awaited_once_with("c-1")
        # The fallback return value (None for an unknown child)
        # is what the caller sees.
        assert result is None

    async def test_no_fallback_on_success(self) -> None:
        primary = AsyncMock()
        primary.get_started_at.return_value = None
        fallback = AsyncMock()
        store = FallbackSessionStore(primary, fallback)
        await store.get_started_at("c-1")
        assert not store.in_fallback
        fallback.get_started_at.assert_not_called()

    async def test_persistent_fallback(self) -> None:
        """Once flipped to fallback, stays there for the
        process lifetime even if the primary 'recovers'."""
        primary = AsyncMock()
        primary.get_started_at.side_effect = ConnectionError("down")
        primary.set_started_at.side_effect = ConnectionError("down")
        fallback = InMemorySessionStore()
        store = FallbackSessionStore(primary, fallback)

        # First call flips to fallback
        await store.get_started_at("c-1")
        assert store.in_fallback

        # Even if primary now returns OK, we stay on fallback
        primary.get_started_at.side_effect = None
        primary.get_started_at.return_value = None
        await store.get_started_at("c-2")
        # primary.get_started_at was called once (the first time)
        # and then we went to fallback for c-2 — so total 1 call
        # to primary.
        assert primary.get_started_at.await_count == 1


# ---------------------------------------------------------------------------
# SyncSessionStore
# ---------------------------------------------------------------------------


class TestSyncSessionStore:
    def test_wraps_async_store(self) -> None:
        """SyncSessionStore bridges an async store to a sync
        interface (used by SessionLimiter). The inner must be
        an async store (RedisSessionStore or AsyncMock)."""
        inner = AsyncMock()
        inner.get_started_at = AsyncMock(return_value=datetime(2026, 7, 22, tzinfo=timezone.utc))
        store = SyncSessionStore(inner)
        result = store.get_started_at("c-1")
        inner.get_started_at.assert_awaited_once_with("c-1")
        assert result is not None
        assert result.year == 2026

    def test_delegates_via_attribute(self) -> None:
        """Verify the wrapper correctly forwards each method."""
        inner = AsyncMock()
        store = SyncSessionStore(inner)
        store.mark_soft_warned("c-1")
        inner.mark_soft_warned.assert_awaited_once_with("c-1")


# ---------------------------------------------------------------------------
# SessionLimiter integration
# ---------------------------------------------------------------------------


class TestSessionLimiterWithStore:
    def test_legacy_in_memory_behaviour_preserved(self) -> None:
        """Default SessionLimiter (no store arg) should behave
        exactly like before — the legacy tests already cover
        this; this is a smoke test post-refactor."""
        from niki.session.limiter import SessionLimiter

        limiter = SessionLimiter(daily_limit_minutes=60)

        assert not limiter.is_started("c-1")
        limiter.start_session("c-1")
        assert limiter.is_started("c-1")
        stats = limiter.end_session("c-1", ExitReason.CHILD_BYE)
        assert stats is not None
        assert not limiter.is_started("c-1")

    def test_custom_store_injection(self) -> None:
        """Injecting a custom store routes state through it."""
        from niki.session.limiter import SessionLimiter

        store = InMemorySessionStore()
        limiter = SessionLimiter(daily_limit_minutes=60, store=store)
        limiter.start_session("c-1")
        # Session state lives in `store`, not on the limiter
        assert store.get_started_at("c-1") is not None
        assert limiter.is_started("c-1")

    def test_two_limiters_share_state_via_store(self) -> None:
        """The whole point of BE-SESSION-redis: two limiters
        (think: two uvicorn workers) sharing the same store
        see the same session state."""
        from niki.session.limiter import SessionLimiter

        store = InMemorySessionStore()
        worker_a = SessionLimiter(store=store)
        worker_b = SessionLimiter(store=store)

        worker_a.start_session("c-1")
        # Worker B sees the session that Worker A started.
        assert worker_b.is_started("c-1")
        # Worker A marks warned, Worker B sees it.
        worker_a.mark_warned("c-1")
        assert worker_b.should_warn("c-1") is False  # already warned

    def test_record_activity_lazily_starts_session(
        self,
    ) -> None:
        from niki.session.limiter import SessionLimiter

        limiter = SessionLimiter(store=InMemorySessionStore())
        limiter.record_activity("c-1")
        assert limiter.is_started("c-1")

    def test_end_session_clears_state(self) -> None:
        from niki.session.limiter import SessionLimiter

        store = InMemorySessionStore()
        limiter = SessionLimiter(store=store)
        limiter.start_session("c-1")
        limiter.end_session("c-1", ExitReason.HARD_LIMIT)
        # All state cleared
        assert store.get_started_at("c-1") is None
        assert store.get_last_activity("c-1") is None
        assert not store.is_soft_warned("c-1")
        # End reason was recorded
        assert store.get_ended("c-1") == "hard_limit"
