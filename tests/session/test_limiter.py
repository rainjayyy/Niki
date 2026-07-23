"""Tests for the session limiter (daily time cap)."""

from datetime import datetime, timedelta

import pytest

from niki.session.limiter import (
    DEFAULT_DAILY_LIMIT_MINUTES,
    ExitReason,
    SessionLimiter,
    SessionStats,
)


@pytest.fixture
def limiter() -> SessionLimiter:
    return SessionLimiter(daily_limit_minutes=60)


class TestSessionBasics:
    def test_default_limit(self, limiter: SessionLimiter) -> None:
        assert limiter.daily_limit_minutes == 60

    def test_initial_state(self, limiter: SessionLimiter) -> None:
        assert not limiter.is_started("c1")
        assert limiter.elapsed_minutes("c1") == 0.0
        assert limiter.idle_minutes("c1") == 0.0

    def test_default_limit_constant(self) -> None:
        assert DEFAULT_DAILY_LIMIT_MINUTES == 60


class TestSessionLifecycle:
    def test_start_session(self, limiter: SessionLimiter) -> None:
        limiter.start_session("c1")
        assert limiter.is_started("c1")
        assert limiter.elapsed_minutes("c1") >= 0

    def test_record_activity(self, limiter: SessionLimiter) -> None:
        limiter.record_activity("c1")
        assert limiter.is_started("c1")
        assert limiter.idle_minutes("c1") < 0.1  # Just started

    def test_end_session_returns_stats(self, limiter: SessionLimiter) -> None:
        limiter.start_session("c1")
        limiter.record_activity("c1")
        stats = limiter.end_session("c1", reason=ExitReason.CHILD_BYE)
        assert stats is not None
        assert stats.child_id == "c1"
        assert stats.exit_reason == ExitReason.CHILD_BYE
        assert stats.duration_seconds >= 0

    def test_end_session_when_no_session(self, limiter: SessionLimiter) -> None:
        assert limiter.end_session("never", reason=ExitReason.CHILD_BYE) is None

    def test_start_clears_ended_state(self, limiter: SessionLimiter) -> None:
        limiter.start_session("c1")
        limiter.end_session("c1", reason=ExitReason.CHILD_BYE)
        assert limiter.is_ended("c1") == ExitReason.CHILD_BYE

        # Re-start
        limiter.start_session("c1")
        assert limiter.is_ended("c1") is None
        assert limiter.is_started("c1")


class TestSoftWarning:
    def test_no_warn_when_no_session(self, limiter: SessionLimiter) -> None:
        assert not limiter.should_warn("c1")

    def test_no_warn_at_start(self, limiter: SessionLimiter) -> None:
        """At session start, no warning needed (we're at 0 min)."""
        limiter.start_session("c1")
        assert not limiter.should_warn("c1")

    def test_warn_at_5_minutes_left(self) -> None:
        # Use a limiter with 10 min limit
        lim = SessionLimiter(daily_limit_minutes=10)
        # Simulate 5 minutes elapsed (warning threshold = 10 - 5 = 5)
        past = datetime(2026, 7, 22, 10, 0, 0)
        now = past + timedelta(minutes=5)
        lim.start_session("c1", now=past)
        assert lim.should_warn("c1", now=now)

    def test_warn_only_once(self, limiter: SessionLimiter) -> None:
        past = datetime(2026, 7, 22, 10, 0, 0)
        now_55 = past + timedelta(minutes=55)
        limiter.start_session("c1", now=past)
        # 55 min elapsed → should warn (5 min before 60)
        assert limiter.should_warn("c1", now=now_55)
        # Mark warned
        limiter.mark_warned("c1")
        # Should not warn again
        assert not limiter.should_warn("c1", now=now_55)


class TestHardLimit:
    def test_not_exhausted_at_start(self, limiter: SessionLimiter) -> None:
        limiter.start_session("c1")
        assert not limiter.is_exhausted("c1")

    def test_exhausted_at_limit(self) -> None:
        lim = SessionLimiter(daily_limit_minutes=10)
        past = datetime(2026, 7, 22, 10, 0, 0)
        now = past + timedelta(minutes=10)
        lim.start_session("c1", now=past)
        assert lim.is_exhausted("c1", now=now)

    def test_exhausted_past_limit(self) -> None:
        lim = SessionLimiter(daily_limit_minutes=10)
        past = datetime(2026, 7, 22, 10, 0, 0)
        now = past + timedelta(minutes=20)
        lim.start_session("c1", now=past)
        assert lim.is_exhausted("c1", now=now)


class TestIdleTimeout:
    def test_not_idle_at_start(self, limiter: SessionLimiter) -> None:
        limiter.start_session("c1")
        assert not limiter.is_idle_timeout("c1")

    def test_idle_after_max_idle(self) -> None:
        lim = SessionLimiter(daily_limit_minutes=60, max_idle_minutes=5)
        past = datetime(2026, 7, 22, 10, 0, 0)
        now = past + timedelta(minutes=6)
        lim.start_session("c1", now=past)
        assert lim.is_idle_timeout("c1", now=now)

    def test_record_activity_resets_idle(self, limiter: SessionLimiter) -> None:
        past = datetime(2026, 7, 22, 10, 0, 0)
        now_later = past + timedelta(minutes=10)
        limiter.start_session("c1", now=past)
        # Record activity → resets idle
        limiter.record_activity("c1", now=now_later)
        # Now even though it's been 10 min, idle is 0
        assert limiter.idle_minutes("c1", now=now_later) < 0.1


class TestRemaining:
    def test_full_remaining_at_start(self, limiter: SessionLimiter) -> None:
        assert limiter.get_remaining_minutes("c1") == 60.0

    def test_remaining_decreases(self) -> None:
        lim = SessionLimiter(daily_limit_minutes=10)
        past = datetime(2026, 7, 22, 10, 0, 0)
        now = past + timedelta(minutes=3)
        lim.start_session("c1", now=past)
        assert lim.get_remaining_minutes("c1", now=now) == 7.0

    def test_remaining_capped_at_zero(self) -> None:
        lim = SessionLimiter(daily_limit_minutes=10)
        past = datetime(2026, 7, 22, 10, 0, 0)
        now = past + timedelta(minutes=20)
        lim.start_session("c1", now=past)
        assert lim.get_remaining_minutes("c1", now=now) == 0.0


class TestSessionStats:
    def test_to_dict(self) -> None:
        stats = SessionStats(
            child_id="c1",
            started_at=datetime(2026, 7, 22, 10, 0, 0),
            ended_at=datetime(2026, 7, 22, 10, 5, 0),
            duration_seconds=300.0,
            exit_reason=ExitReason.CHILD_BYE,
        )
        d = stats.to_dict()
        assert d["child_id"] == "c1"
        assert d["duration_seconds"] == 300.0
        assert d["duration_minutes"] == 5.0
        assert d["exit_reason"] == "child_bye"


class TestExitReason:
    def test_all_reasons(self) -> None:
        assert ExitReason.HARD_LIMIT.value == "hard_limit"
        assert ExitReason.PARENT_PAUSE.value == "parent_pause"
        assert ExitReason.PARENT_DELETE.value == "parent_delete"
        assert ExitReason.CHILD_BYE.value == "child_bye"
        assert ExitReason.IDLE_TIMEOUT.value == "idle_timeout"
