"""Model resilience — PRD #57-#60.

Public surface:
    CircuitBreaker         — sliding-window state machine (CLOSED/OPEN/HALF_OPEN)
    CircuitState          — state enum
    BreakerDecision       — decision returned by the breaker
    CircuitBreakerAwareClient — wraps an LLM client, applies degradation
    DegradationNotifier   — sends silent signal to parent dashboard
"""

from niki.resilience.breaker import (
    BreakerDecision,
    BreakerStats,
    CircuitBreaker,
    CircuitEvent,
    CircuitState,
)
from niki.resilience.client import CircuitBreakerAwareClient
from niki.resilience.notifier import DegradationNotifier

__all__ = [
    "BreakerDecision",
    "BreakerStats",
    "CircuitBreaker",
    "CircuitEvent",
    "CircuitState",
    "CircuitBreakerAwareClient",
    "DegradationNotifier",
]
