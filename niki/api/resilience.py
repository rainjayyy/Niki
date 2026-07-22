"""Resilience API endpoints — PRD #57-#60.

Demonstration endpoints for the #10 vertical slice:
  GET  /resilience/breaker/stats        — current breaker stats
  POST /resilience/breaker/reset       — manually reset to CLOSED
  POST /resilience/breaker/force-open  — manually open (for testing)
  GET  /resilience/degradation         — current degradation state
  POST /resilience/degradation/recover — mark recovered
  POST /resilience/chat                — example: chat via circuit-breaker-aware client
"""

from __future__ import annotations

from fastapi import APIRouter

from niki.resilience import (
    CircuitBreaker,
    CircuitBreakerAwareClient,
    DegradationNotifier,
)

router = APIRouter(prefix="/resilience", tags=["resilience"])

# Module-level singletons
_breaker = CircuitBreaker()
_notifier = DegradationNotifier()

# Stub DeepSeek that fails (to trigger breaker)
class _FailingDeepSeek:
    async def chat(
        self, messages, temperature=0.7, max_tokens=512, **kwargs
    ):
        raise RuntimeError("simulated DeepSeek-V4 outage")

# Stub DeepSeek that succeeds
class _WorkingDeepSeek:
    async def chat(
        self, messages, temperature=0.7, max_tokens=512, **kwargs
    ):
        return {
            "content": "DeepSeek-V4 working normally",
            "model": "deepseek-v4",
            "usage": {"total_tokens": 10},
            "finish_reason": "stop",
        }

# Stub doubao fallback
class _StubDoubao:
    async def chat(
        self, messages, temperature=0.7, max_tokens=512, **kwargs
    ):
        return {
            "content": "doubao fallback working (slightly simpler)",
            "model": "doubao-lite",
            "usage": {"total_tokens": 5},
            "finish_reason": "stop",
        }

# Default: working primary + fallback
_client = CircuitBreakerAwareClient(
    primary=_WorkingDeepSeek(),
    fallback=_StubDoubao(),
    breaker=_breaker,
    notifier=_notifier,
)


@router.get("/breaker/stats")
async def breaker_stats() -> dict:
    """Get current circuit-breaker statistics."""
    return _breaker.get_stats().to_dict()


@router.post("/breaker/reset")
async def breaker_reset() -> dict:
    """Manually reset the breaker to CLOSED."""
    _breaker.reset()
    await _notifier.notify_recovered()
    return _breaker.get_stats().to_dict()


@router.post("/breaker/force-open")
async def breaker_force_open() -> dict:
    """Manually trip the breaker (for testing/admin)."""
    _breaker.force_open("manual admin trigger")
    return _breaker.get_stats().to_dict()


@router.get("/degradation")
async def degradation_state() -> dict:
    """Get current degradation state (for the parent dashboard)."""
    return _notifier.state.to_dict()


@router.post("/degradation/recover")
async def degradation_recover() -> dict:
    """Mark degradation as recovered (e.g. when breaker closes)."""
    await _notifier.notify_recovered()
    return _notifier.state.to_dict()


@router.post("/chat")
async def resilient_chat(payload: dict) -> dict:
    """Example call via the circuit-breaker-aware client.

    Request body: {"messages": [{"role": "user", "content": "..."}]}

    Tries the primary LLM first; falls back to doubao on failure.
    """
    messages = payload.get("messages", [])
    if not messages:
        return {"error": "no messages provided"}

    result = await _client.chat(messages=messages)
    return {
        "result": result,
        "breaker_state": _breaker.get_stats().to_dict(),
        "degradation": _notifier.state.to_dict(),
    }


@router.post("/simulate-outage")
async def simulate_outage() -> dict:
    """Simulate a DeepSeek-V4 outage by switching to the failing client.

    This causes the next chat calls to fall back to doubao
    (and the breaker to trip after enough failures).
    """
    global _client
    _client = CircuitBreakerAwareClient(
        primary=_FailingDeepSeek(),
        fallback=_StubDoubao(),
        breaker=_breaker,
        notifier=_notifier,
    )
    return {"status": "DeepSeek-V4 now failing — calls will use fallback"}


@router.post("/simulate-recovery")
async def simulate_recovery() -> dict:
    """Switch back to a working DeepSeek-V4 client."""
    global _client
    _client = CircuitBreakerAwareClient(
        primary=_WorkingDeepSeek(),
        fallback=_StubDoubao(),
        breaker=_breaker,
        notifier=_notifier,
    )
    _breaker.reset()
    return {"status": "DeepSeek-V4 restored — breaker reset"}
