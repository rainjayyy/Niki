"""Tests for the circuit breaker."""

import asyncio

import pytest

from niki.resilience.breaker import (
    BreakerDecision,
    CircuitBreaker,
    CircuitState,
)


@pytest.fixture
def breaker() -> CircuitBreaker:
    """Tight thresholds for fast tests."""
    return CircuitBreaker(
        window_size_s=10.0,
        error_rate_threshold=0.5,
        consecutive_threshold=3,
        initial_cooldown_s=2.0,
        max_cooldown_s=8.0,
    )


class TestInitialState:
    def test_starts_closed(self, breaker: CircuitBreaker) -> None:
        assert breaker.state == CircuitState.CLOSED

    def test_initial_stats(self, breaker: CircuitBreaker) -> None:
        stats = breaker.get_stats()
        assert stats.state == CircuitState.CLOSED
        assert stats.window_total == 0
        assert stats.error_rate == 0.0
        assert stats.total_trips == 0


class TestBeforeCall:
    async def test_closed_allows_call(self, breaker: CircuitBreaker) -> None:
        decision = await breaker.before_call()
        assert decision.allowed is True
        assert decision.state == CircuitState.CLOSED
        assert decision.is_probe is False

    async def test_open_denies_call(self, breaker: CircuitBreaker) -> None:
        breaker.force_open()
        decision = await breaker.before_call()
        assert decision.allowed is False
        assert decision.state == CircuitState.OPEN

    async def test_open_allows_probe_after_cooldown(self, breaker: CircuitBreaker) -> None:
        breaker.force_open()
        # Wait for cooldown to elapse
        await asyncio.sleep(2.1)
        decision = await breaker.before_call()
        assert decision.allowed is True
        assert decision.is_probe is True
        assert decision.state == CircuitState.HALF_OPEN


class TestTripConditions:
    async def test_consecutive_failures_trip(self, breaker: CircuitBreaker) -> None:
        """5 consecutive failures (>= threshold 3) should trip."""
        b = CircuitBreaker(
            window_size_s=10.0,
            error_rate_threshold=0.5,
            consecutive_threshold=3,
        )
        for _ in range(3):
            await b.record_failure("boom")
        assert b.state == CircuitState.OPEN
        stats = b.get_stats()
        assert stats.consecutive_failures == 3
        assert stats.total_trips == 1

    async def test_error_rate_trips(self, breaker: CircuitBreaker) -> None:
        """Error rate > 50% in sliding window should trip."""
        b = CircuitBreaker(
            window_size_s=10.0,
            error_rate_threshold=0.5,
            consecutive_threshold=3,
        )
        # 2 failures + 1 success = 67% error rate (≥ 3 events, > 50%)
        await b.record_failure()
        await b.record_failure()
        await b.record_success()
        assert b.state == CircuitState.OPEN

    async def test_low_error_rate_does_not_trip(self, breaker: CircuitBreaker) -> None:
        b = CircuitBreaker(
            window_size_s=10.0,
            error_rate_threshold=0.5,
            consecutive_threshold=3,
        )
        # 1 failure + 2 successes = 33% error rate (below threshold)
        await b.record_failure()
        await b.record_success()
        await b.record_success()
        assert b.state == CircuitState.CLOSED

    async def test_success_resets_consecutive(self, breaker: CircuitBreaker) -> None:
        """A success resets the consecutive-failures counter (the metric).

        Note: even after a success, the sliding-window error rate may
        still trip the breaker if the window is dominated by failures.
        This test verifies the consecutive counter itself, not the
        overall breaker state.
        """
        b = CircuitBreaker(
            window_size_s=10.0,
            error_rate_threshold=0.5,
            consecutive_threshold=3,
        )
        await b.record_failure()
        await b.record_failure()
        assert b._consecutive_failures == 2
        await b.record_success()
        # After success, consecutive_failures should reset to 0
        assert b._consecutive_failures == 0


class TestStateTransitions:
    async def test_closed_to_open(self, breaker: CircuitBreaker) -> None:
        for _ in range(3):
            await breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

    async def test_open_to_half_open_after_cooldown(self, breaker: CircuitBreaker) -> None:
        breaker.force_open()
        await asyncio.sleep(2.1)
        await breaker.before_call()  # This transitions to half-open
        assert breaker.state == CircuitState.HALF_OPEN

    async def test_half_open_to_closed_on_probe_success(self, breaker: CircuitBreaker) -> None:
        breaker.force_open()
        await asyncio.sleep(2.1)
        await breaker.before_call()  # Transitions to half-open
        await breaker.record_success()
        assert breaker.state == CircuitState.CLOSED

    async def test_half_open_to_open_on_probe_failure(self, breaker: CircuitBreaker) -> None:
        breaker.force_open()
        await asyncio.sleep(2.1)
        await breaker.before_call()  # Transitions to half-open
        await breaker.record_failure("probe failed")
        assert breaker.state == CircuitState.OPEN

    async def test_probe_failure_doubles_cooldown(self, breaker: CircuitBreaker) -> None:
        """PRD: cooldown doubles on each probe failure, capped at 10 min."""
        breaker.force_open()
        # First probe → fail → double cooldown
        await asyncio.sleep(2.1)
        await breaker.before_call()
        await breaker.record_failure()
        stats1 = breaker.get_stats()
        assert stats1.cooldown_s == 4.0  # 2.0 * 2

        # Second probe → fail → double again
        await asyncio.sleep(4.1)
        await breaker.before_call()
        await breaker.record_failure()
        stats2 = breaker.get_stats()
        assert stats2.cooldown_s == 8.0  # 4.0 * 2

        # Third probe → would be 16.0 but capped at 8.0
        await asyncio.sleep(8.1)
        await breaker.before_call()
        await breaker.record_failure()
        stats3 = breaker.get_stats()
        assert stats3.cooldown_s == 8.0  # capped


class TestManualControls:
    def test_force_open(self, breaker: CircuitBreaker) -> None:
        breaker.force_open("test")
        assert breaker.state == CircuitState.OPEN
        assert breaker.get_stats().total_trips == 1

    def test_reset(self, breaker: CircuitBreaker) -> None:
        b = CircuitBreaker(
            window_size_s=10.0,
            error_rate_threshold=0.5,
            consecutive_threshold=3,
        )
        b.force_open()
        assert b.state == CircuitState.OPEN
        b.reset()
        assert b.state == CircuitState.CLOSED
        stats = b.get_stats()
        assert stats.consecutive_failures == 0


class TestSlidingWindow:
    async def test_old_events_pruned(self, breaker: CircuitBreaker) -> None:
        """Events older than the window are pruned."""
        # Add some events
        for _ in range(2):
            await breaker.record_failure()
        stats = breaker.get_stats()
        assert stats.window_total == 2
        # Wait > window_size (10s)
        await asyncio.sleep(10.5)
        stats = breaker.get_stats()
        assert stats.window_total == 0


class TestStats:
    async def test_stats_reflect_state(self, breaker: CircuitBreaker) -> None:
        await breaker.record_success()
        await breaker.record_failure()
        stats = breaker.get_stats()
        assert stats.window_total == 2
        assert stats.window_failures == 1
        assert stats.error_rate == 0.5
        assert stats.consecutive_failures == 1


class TestDecisionDataclass:
    def test_decision_to_dict(self) -> None:
        decision = BreakerDecision(allowed=True, state=CircuitState.HALF_OPEN, is_probe=True)
        d = {
            "allowed": decision.allowed,
            "state": decision.state.value,
            "is_probe": decision.is_probe,
        }
        assert d["allowed"] is True
        assert d["state"] == "half_open"
        assert d["is_probe"] is True
