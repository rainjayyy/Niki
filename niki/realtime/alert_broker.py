"""Process-wide alert broker for the SSE realtime channel (BE-RT-002).

A simple in-process pub/sub keyed by `parent_id`. The
`AuthAuditLogger` (BE-AUTH-001-rollout-alerts) calls
`publish(alert)` whenever a SecurityAlert is raised; the SSE
endpoint `GET /alerts/stream/{child_id}` calls `subscribe(...)`
and yields alerts as they arrive.

Multi-process deployments need a Redis pub/sub broker (future
work). Single-process is enough for the v1 deployment.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import structlog

from niki.security.alerts import SecurityAlert

logger = structlog.get_logger(__name__)


# Cap the per-subscriber queue to prevent runaway memory
# growth when a client is wedged but still TCP-connected.
MAX_QUEUE_SIZE: int = 100


class AlertBroker:
    """Fan-out from SecurityAlert publishers to per-parent
    SSE subscribers."""

    def __init__(self, *, max_queue_size: int = MAX_QUEUE_SIZE) -> None:
        # parent_id → set[asyncio.Queue]. We allow multiple
        # subscribers per parent (e.g. parent opens dashboard in
        # two tabs).
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._max_queue_size = max_queue_size
        self._lock = threading.Lock()

    def subscribe(self, parent_id: str) -> asyncio.Queue:
        """Register a new subscriber. Returns a queue the SSE
        endpoint should drain."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue_size)
        with self._lock:
            self._subscribers.setdefault(parent_id, set()).add(q)
        return q

    def unsubscribe(self, parent_id: str, queue: asyncio.Queue) -> None:
        """Drop a subscriber (called from SSE endpoint's finally)."""
        with self._lock:
            subs = self._subscribers.get(parent_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(parent_id, None)

    def publish(self, alert: SecurityAlert) -> int:
        """Push an alert to every subscriber whose parent_id
        matches `alert.actor_parent_id`. Returns the number of
        subscribers that accepted the alert."""
        target = alert.actor_parent_id
        if target is None:
            return 0
        with self._lock:
            subs = list(self._subscribers.get(target, ()))
        accepted = 0
        for q in subs:
            try:
                q.put_nowait(alert)
                accepted += 1
            except asyncio.QueueFull:
                logger.warning(
                    "alert.broker.queue_full",
                    parent_id=target,
                    alert_type=alert.alert_type,
                )
        return accepted

    def subscriber_count(self, parent_id: str | None = None) -> int:
        if parent_id is None:
            with self._lock:
                return sum(
                    len(s) for s in self._subscribers.values()
                )
        with self._lock:
            return len(self._subscribers.get(parent_id, ()))


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_broker: AlertBroker | None = None
_broker_lock = threading.Lock()


def get_alert_broker() -> AlertBroker:
    global _broker
    with _broker_lock:
        if _broker is None:
            _broker = AlertBroker()
        return _broker


def set_alert_broker(broker: AlertBroker | None) -> None:
    """Override the process-wide broker (tests)."""
    global _broker
    with _broker_lock:
        _broker = broker