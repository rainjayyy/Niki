"""Circuit breaker — PRD #57-#60.

Sliding-window state machine for the DeepSeek-V4 (deep-reasoning) LLM:

  CLOSED      — normal operation, all requests go to DeepSeek-V4
  OPEN        — tripped: too many errors, route everything to doubao
  HALF_OPEN   — after 60s cooldown, allow 1 probe request to test recovery
                 - probe success → CLOSED
                 - probe failure → OPEN (cooldown doubles, capped at 10 min)

Trip conditions (PRD #58):
  - 30s window, error rate > 50%, OR
  - 5 consecutive failures

State machine:
  CLOSED ─── error threshold ───▶ OPEN
    ▲                              │
    │                              │ 60s cooldown
    │                              ▼
    └──── probe success ──── HALF_OPEN
                                │
                                └── probe failure → OPEN (cooldown × 2, cap 10min)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class CircuitState(StrEnum):
    """The three states of the circuit breaker."""

    CLOSED = "closed"              # normal
    OPEN = "open"                  # tripped, fail-fast to fallback
    HALF_OPEN = "half_open"        # testing recovery


@dataclass(frozen=True)
class CircuitEvent:
    """A single observed event in the sliding window."""

    timestamp: float               # monotonic time
    success: bool
    error: str | None = None


@dataclass(frozen=True)
class BreakerStats:
    """A snapshot of the breaker's current stats."""

    state: CircuitState
    window_size_s: float
    window_total: int
    window_failures: int
    error_rate: float              # 0.0 to 1.0
    consecutive_failures: int
    last_change: datetime
    cooldown_s: float
    cooldown_remaining_s: float
    total_trips: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "window_size_s": self.window_size_s,
            "window_total": self.window_total,
            "window_failures": self.window_failures,
            "error_rate": round(self.error_rate, 3),
            "consecutive_failures": self.consecutive_failures,
            "last_change": self.last_change.isoformat(),
            "cooldown_s": self.cooldown_s,
            "cooldown_remaining_s": round(max(0.0, self.cooldown_remaining_s), 2),
            "total_trips": self.total_trips,
        }


@dataclass(frozen=True)
class BreakerDecision:
    """Decision returned by the breaker for a single call."""

    allowed: bool
    state: CircuitState
    is_probe: bool = False
    reason: str = ""


class CircuitBreaker:
    """Sliding-window circuit breaker.

    Designed to wrap a single high-quality (expensive) LLM client.
    When tripped, callers should fail-fast to a fallback (e.g. doubao).
    """

    def __init__(
        self,
        window_size_s: float = 30.0,
        error_rate_threshold: float = 0.5,
        consecutive_threshold: int = 5,
        initial_cooldown_s: float = 60.0,
        max_cooldown_s: float = 600.0,    # 10 minutes cap
    ) -> None:
        self.window_size_s = window_size_s
        self.error_rate_threshold = error_rate_threshold
        self.consecutive_threshold = consecutive_threshold
        self.initial_cooldown_s = initial_cooldown_s
        self.max_cooldown_s = max_cooldown_s

        self._state: CircuitState = CircuitState.CLOSED
        self._events: deque[CircuitEvent] = deque()
        self._consecutive_failures: int = 0
        self._opened_at: float | None = None
        self._current_cooldown: float = initial_cooldown_s
        self._last_change: datetime = datetime.utcnow()
        self._total_trips: int = 0
        self._half_open_in_flight: bool = False
        # Lock for state transitions
        self._lock = asyncio.Lock()

    # ---- State machine ----

    @property
    def state(self) -> CircuitState:
        return self._state

    def get_stats(self) -> BreakerStats:
        """Get a snapshot of the current state and window stats."""
        now = time.monotonic()
        self._prune(now)
        total = len(self._events)
        failures = sum(1 for e in self._events if not e.success)
        error_rate = (failures / total) if total > 0 else 0.0
        cooldown_remaining = 0.0
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            elapsed = now - self._opened_at
            cooldown_remaining = max(0.0, self._current_cooldown - elapsed)
        return BreakerStats(
            state=self._state,
            window_size_s=self.window_size_s,
            window_total=total,
            window_failures=failures,
            error_rate=error_rate,
            consecutive_failures=self._consecutive_failures,
            last_change=self._last_change,
            cooldown_s=self._current_cooldown,
            cooldown_remaining_s=cooldown_remaining,
            total_trips=self._total_trips,
        )

    async def before_call(self) -> BreakerDecision:
        """Check the breaker before making a call.

        Returns:
            BreakerDecision — `allowed=True` means proceed with primary.
            `allowed=False` means fail-fast to fallback.
        """
        async with self._lock:
            now = time.monotonic()
            self._prune(now)

            if self._state == CircuitState.CLOSED:
                return BreakerDecision(allowed=True, state=self._state)

            if self._state == CircuitState.OPEN:
                # Check if cooldown has elapsed
                if self._opened_at is None:
                    return BreakerDecision(allowed=True, state=self._state)
                elapsed = now - self._opened_at
                if elapsed >= self._current_cooldown:
                    # Transition to half-open
                    self._transition(CircuitState.HALF_OPEN)
                    self._half_open_in_flight = True
                    return BreakerDecision(
                        allowed=True,
                        state=self._state,
                        is_probe=True,
                        reason="cooldown elapsed — allowing probe",
                    )
                return BreakerDecision(
                    allowed=False,
                    state=self._state,
                    reason=f"open; cooldown remaining: {self._current_cooldown - elapsed:.1f}s",
                )

            # HALF_OPEN: only one probe at a time
            if self._half_open_in_flight:
                return BreakerDecision(
                    allowed=False,
                    state=self._state,
                    reason="probe in flight",
                )
            self._half_open_in_flight = True
            return BreakerDecision(
                allowed=True,
                state=self._state,
                is_probe=True,
                reason="probe request",
            )

    async def record_success(self) -> None:
        """Record a successful call (closes breaker if probe succeeded)."""
        async with self._lock:
            now = time.monotonic()
            self._events.append(CircuitEvent(timestamp=now, success=True))
            self._consecutive_failures = 0

            if self._state == CircuitState.HALF_OPEN:
                # Probe succeeded → close
                self._half_open_in_flight = False
                self._current_cooldown = self.initial_cooldown_s
                self._opened_at = None
                self._transition(CircuitState.CLOSED)
                logger.info("breaker.probe_succeeded", cooldown_reset=True)
            elif self._state == CircuitState.OPEN:
                # Shouldn't happen if before_call works correctly,
                # but be defensive
                self._half_open_in_flight = False
            else:
                # CLOSED state — also re-check trip conditions after
                # the event was added (window may have shifted)
                if self._should_trip():
                    self._opened_at = now
                    self._total_trips += 1
                    self._transition(CircuitState.OPEN)
                    logger.warning(
                        "breaker.tripped_after_success",
                        window_total=len(self._events),
                        window_failures=sum(1 for e in self._events if not e.success),
                    )

    async def record_failure(self, error: str = "") -> None:
        """Record a failed call (may trip the breaker)."""
        async with self._lock:
            now = time.monotonic()
            self._events.append(
                CircuitEvent(timestamp=now, success=False, error=error)
            )
            self._consecutive_failures += 1

            if self._state == CircuitState.HALF_OPEN:
                # Probe failed → reopen with doubled cooldown (cap 10min)
                self._half_open_in_flight = False
                self._current_cooldown = min(
                    self._current_cooldown * 2, self.max_cooldown_s
                )
                self._opened_at = now
                self._transition(CircuitState.OPEN)
                logger.warning(
                    "breaker.probe_failed",
                    new_cooldown_s=self._current_cooldown,
                )
                return

            # CLOSED state — check trip conditions
            if self._should_trip():
                self._opened_at = now
                self._total_trips += 1
                self._transition(CircuitState.OPEN)
                logger.warning(
                    "breaker.tripped",
                    consecutive=self._consecutive_failures,
                    window_total=len(self._events),
                    window_failures=sum(1 for e in self._events if not e.success),
                )

    # ---- Internals ----

    def _should_trip(self) -> bool:
        """Check if the breaker should trip based on current window."""
        # Consecutive failure threshold (fast-trip)
        if self._consecutive_failures >= self.consecutive_threshold:
            return True

        # Error rate threshold (sliding window)
        if len(self._events) >= self.consecutive_threshold:
            failures = sum(1 for e in self._events if not e.success)
            error_rate = failures / len(self._events)
            if error_rate > self.error_rate_threshold:
                return True

        return False

    def _prune(self, now: float) -> None:
        """Remove events outside the sliding window."""
        cutoff = now - self.window_size_s
        while self._events and self._events[0].timestamp < cutoff:
            self._events.popleft()

    def _transition(self, new_state: CircuitState) -> None:
        """Change state (internal; lock must be held)."""
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        self._last_change = datetime.utcnow()
        logger.info(
            "breaker.transition",
            from_state=old.value,
            to_state=new_state.value,
        )

    def force_open(self, reason: str = "manual") -> None:
        """Manually open the breaker (for testing/admin)."""
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._total_trips += 1
        self._last_change = datetime.utcnow()
        logger.warning("breaker.force_open", reason=reason)

    def reset(self) -> None:
        """Manually reset the breaker to closed (for testing/admin)."""
        self._state = CircuitState.CLOSED
        self._events.clear()
        self._consecutive_failures = 0
        self._opened_at = None
        self._current_cooldown = self.initial_cooldown_s
        self._half_open_in_flight = False
        self._last_change = datetime.utcnow()
        logger.info("breaker.reset")
