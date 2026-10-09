"""Sliding-window violation rate tracker (BE-AUTH-001-rollout-alerts).

Tracks the recent count of lateral-auth violations per actor.
In-memory, single-process. Multi-process deployments should swap
this out for a Redis-backed implementation (BE-API-002-redis).

Thread-safe: all public methods take an internal lock so multiple
worker threads on the same process can't race the prune step.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Deque


class ViolationRateTracker:
    """Track recent violation timestamps per actor_parent_id.

    Args:
        window_seconds: events older than this are pruned on
            every `record()` call.
        burst_count: the threshold above which `should_alert()`
            returns True.
    """

    def __init__(
        self,
        *,
        window_seconds: int = 300,
        burst_count: int = 3,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if burst_count <= 0:
            raise ValueError("burst_count must be positive")
        self.window_seconds = window_seconds
        self.burst_count = burst_count
        self._events: dict[str, Deque[datetime]] = {}
        self._lock = Lock()

    def record(
        self,
        actor_parent_id: str,
        *,
        now: datetime | None = None,
    ) -> int:
        """Record a violation. Returns the count within the window.

        Prunes events older than `window_seconds` from `now` first,
        so the returned count is always current.
        """
        if not actor_parent_id:
            return 0
        ts = now or datetime.now(timezone.utc)
        cutoff = ts - timedelta(seconds=self.window_seconds)
        with self._lock:
            bucket = self._events.setdefault(actor_parent_id, deque())
            # Prune head entries that have fallen out of the window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            bucket.append(ts)
            return len(bucket)

    def should_alert(self, actor_parent_id: str) -> bool:
        """True iff the actor is at or above the burst threshold."""
        with self._lock:
            bucket = self._events.get(actor_parent_id)
            if not bucket:
                return False
            return len(bucket) >= self.burst_count

    def count(self, actor_parent_id: str) -> int:
        """Return the current window count (without recording)."""
        with self._lock:
            bucket = self._events.get(actor_parent_id)
            return len(bucket) if bucket else 0

    def reset(self, actor_parent_id: str | None = None) -> None:
        """Clear tracked events. If `actor_parent_id` is None,
        reset everything (useful for tests)."""
        with self._lock:
            if actor_parent_id is None:
                self._events.clear()
            else:
                self._events.pop(actor_parent_id, None)
