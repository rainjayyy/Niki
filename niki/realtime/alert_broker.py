"""Process-wide alert broker for the SSE realtime channel (BE-RT-002,
BE-RT-002-rollout).

A simple in-process pub/sub keyed by `parent_id`. The
`AuthAuditLogger` (BE-AUTH-001-rollout-alerts) calls
`publish(alert)` whenever a SecurityAlert is raised; the SSE
endpoint `GET /alerts/stream/{child_id}` calls `subscribe(...)`
and yields alerts as they arrive.

BE-RT-002-rollout adds a per-parent ring buffer so that a parent
connecting (or reconnecting) gets a "replay" of recent alerts that
fired before they connected. The frontend listens for `event: replay`
events (separate from `event: alert`) so it can render those as
"history" rather than as new push notifications.

Multi-process deployments need a Redis pub/sub broker (future
work). Single-process is enough for the v1 deployment.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import datetime

import structlog

from niki.security.alerts import SecurityAlert

logger = structlog.get_logger(__name__)


# Cap the per-subscriber queue to prevent runaway memory
# growth when a client is wedged but still TCP-connected.
MAX_QUEUE_SIZE: int = 100


# Cap the per-parent ring buffer for the replay feature. A parent
# reconnecting after the dashboard has been closed for a while
# gets up to `REPLAY_BUFFER_CAP` most-recent alerts for that parent.
# Older alerts fall off the back of the buffer silently.
REPLAY_BUFFER_CAP: int = 500


# Default cap when callers don't specify `limit` in `recent_alerts`.
REPLAY_DEFAULT_LIMIT: int = 50


class AlertBroker:
    """Fan-out from SecurityAlert publishers to per-parent
    SSE subscribers.

    Also keeps a per-parent ring buffer of the most recent
    alerts so newly-connecting subscribers can be replayed
    history.
    """

    def __init__(
        self,
        *,
        max_queue_size: int = MAX_QUEUE_SIZE,
        buffer_cap: int = REPLAY_BUFFER_CAP,
    ) -> None:
        # parent_id → set[asyncio.Queue]. We allow multiple
        # subscribers per parent (e.g. parent opens dashboard in
        # two tabs).
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._max_queue_size = max_queue_size
        # parent_id → deque[SecurityAlert]. Bounded ring buffer
        # of recent alerts for replay on connect.
        self._buffers: dict[str, deque[SecurityAlert]] = {}
        self._buffer_cap = buffer_cap
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
        matches `alert.actor_parent_id`, and append it to the
        per-parent ring buffer. Returns the number of subscribers
        that accepted the alert."""
        target = alert.actor_parent_id
        if target is None:
            return 0
        # 1) Push into the per-parent ring buffer FIRST so that a
        # subscriber that joins in the same tick gets the new
        # alert on its replay (if applicable). The buffer is
        # bounded; oldest entries are silently dropped.
        self._push_to_buffer(target, alert)
        # 2) Fan out to current live subscribers.
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
                return sum(len(s) for s in self._subscribers.values())
        with self._lock:
            return len(self._subscribers.get(parent_id, ()))

    # ------------------------------------------------------------------
    # Replay buffer (BE-RT-002-rollout)
    # ------------------------------------------------------------------

    def _push_to_buffer(self, parent_id: str, alert: SecurityAlert) -> None:
        """Append `alert` to the per-parent ring buffer, dropping
        the oldest entry when full."""
        with self._lock:
            buf = self._buffers.get(parent_id)
            if buf is None:
                buf = deque(maxlen=self._buffer_cap)
                self._buffers[parent_id] = buf
            buf.append(alert)

    def recent_alerts(
        self,
        parent_id: str,
        *,
        alert_type: str | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        limit: int = REPLAY_DEFAULT_LIMIT,
    ) -> list[SecurityAlert]:
        """Return the most recent N alerts for `parent_id` that
        match the given filters, in chronological order
        (oldest-first).

        Filters:
            - `alert_type`: exact-match on the alert's `alert_type`
            - `severity`:   exact-match on the alert's `severity`
            - `since`:      only alerts with `detected_at >= since`

        `limit` caps the number of returned alerts. The buffer is
        bounded by `buffer_cap` so very old alerts are NOT
        available — that's documented in the ticket.
        """
        if limit <= 0:
            return []
        with self._lock:
            buf = self._buffers.get(parent_id)
            snapshot = list(buf) if buf is not None else []
        # Apply filters in chronological order; the buffer is
        # already in append order so iterating forward preserves
        # chronological order.
        out: list[SecurityAlert] = []
        for alert in snapshot:
            if alert_type is not None and alert.alert_type != alert_type:
                continue
            if severity is not None and alert.severity != severity:
                continue
            if since is not None and alert.detected_at < since:
                continue
            out.append(alert)
        # Cap from the most recent side.
        if len(out) > limit:
            out = out[-limit:]
        return out

    def buffer_size(self, parent_id: str) -> int:
        """Diagnostic: how many alerts are buffered for this parent."""
        with self._lock:
            buf = self._buffers.get(parent_id)
            return len(buf) if buf is not None else 0


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
