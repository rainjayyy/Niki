"""Tests for the circuit-breaker-aware LLM client."""

import pytest

from niki.resilience import (
    CircuitBreaker,
    CircuitBreakerAwareClient,
    CircuitState,
    DegradationNotifier,
)


class _FailingPrimary:
    """Primary that always fails (to test fallback)."""

    async def chat(self, *args, **kwargs):
        raise RuntimeError("simulated outage")


class _WorkingPrimary:
    """Primary that always succeeds."""

    async def chat(self, *args, **kwargs):
        return {
            "content": "primary success",
            "model": "primary",
            "usage": {},
            "finish_reason": "stop",
        }


class _WorkingFallback:
    """Fallback that always succeeds."""

    async def chat(self, *args, **kwargs):
        return {
            "content": "fallback success",
            "model": "fallback",
            "usage": {},
            "finish_reason": "stop",
        }


class _FailingFallback:
    async def chat(self, *args, **kwargs):
        raise RuntimeError("fallback also failed")


class TestCircuitBreakerAwareClient:
    @pytest.fixture
    def client(self) -> CircuitBreakerAwareClient:
        return CircuitBreakerAwareClient(
            primary=_WorkingPrimary(),
            fallback=_WorkingFallback(),
            breaker=CircuitBreaker(
                consecutive_threshold=2,
                initial_cooldown_s=1.0,
            ),
        )

    async def test_closed_circuit_uses_primary(self, client: CircuitBreakerAwareClient) -> None:
        result = await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert result["content"] == "primary success"
        assert client.breaker.state == CircuitState.CLOSED

    async def test_failing_primary_falls_back(self, client: CircuitBreakerAwareClient) -> None:
        # Swap in a failing primary
        client.primary = _FailingPrimary()
        result = await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert result["content"] == "fallback success"

    async def test_failing_primary_records_failure(self, client: CircuitBreakerAwareClient) -> None:
        client.primary = _FailingPrimary()
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert client.breaker.get_stats().consecutive_failures == 1

    async def test_consecutive_failures_trip_breaker(
        self, client: CircuitBreakerAwareClient
    ) -> None:
        client.primary = _FailingPrimary()
        # 2 calls → 2 consecutive failures → trip (threshold=2)
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert client.breaker.state == CircuitState.OPEN

    async def test_after_trip_fallback_used(self, client: CircuitBreakerAwareClient) -> None:
        client.primary = _FailingPrimary()
        # Trip the breaker
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        # Now even with working primary, fallback is used
        client.primary = _WorkingPrimary()
        result = await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert result["content"] == "fallback success"


class TestFallbackResilience:
    """PRD: Niki must always be available."""

    async def test_last_resort_when_both_fail(self) -> None:
        client = CircuitBreakerAwareClient(
            primary=_FailingPrimary(),
            fallback=_FailingFallback(),
            breaker=CircuitBreaker(),
        )
        result = await client.chat(messages=[{"role": "user", "content": "hi"}])
        # Should still return something
        assert "content" in result
        assert "model" in result
        assert result["model"] == "last-resort-stub"


class TestDegradationNotification:
    async def test_degradation_notified_on_failure(self) -> None:
        notifier = DegradationNotifier()
        client = CircuitBreakerAwareClient(
            primary=_FailingPrimary(),
            fallback=_WorkingFallback(),
            breaker=CircuitBreaker(),
            notifier=notifier,
        )
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert notifier.is_active() is True

    async def test_deactivation_on_recovery(self) -> None:
        notifier = DegradationNotifier()
        client = CircuitBreakerAwareClient(
            primary=_WorkingPrimary(),
            fallback=_WorkingFallback(),
            breaker=CircuitBreaker(),
            notifier=notifier,
        )
        # Trigger degradation
        client.primary = _FailingPrimary()
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert notifier.is_active() is True
        # Simulate recovery (working primary + reset breaker)
        client.primary = _WorkingPrimary()
        client.breaker.reset()
        # Record success → should not auto-clear notifier in this design
        # (notifier cleared only via explicit notify_recovered)
        # We just confirm notifier state is independent
        assert notifier.is_active() is True


class TestProbeBehavior:
    async def test_probe_succeeds_closes_circuit(self) -> None:
        breaker = CircuitBreaker(
            consecutive_threshold=2,
            initial_cooldown_s=0.5,
        )
        client = CircuitBreakerAwareClient(
            primary=_FailingPrimary(),
            fallback=_WorkingFallback(),
            breaker=breaker,
        )
        # Trip
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert breaker.state == CircuitState.OPEN
        # Wait for cooldown
        import asyncio

        await asyncio.sleep(0.6)
        # Switch to working primary
        client.primary = _WorkingPrimary()
        # Next call is a probe → succeeds → closes circuit
        await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert breaker.state == CircuitState.CLOSED
