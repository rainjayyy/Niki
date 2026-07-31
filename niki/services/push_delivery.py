"""Push delivery abstraction (BE-PUSH-006).

Single seam between Niki and any push-vendor SDK (HMS / MiPush /
OPPO / Vivo / Flyme / APNs / FCM / Web Push). BE-PUSH-002 / 003
will provide real implementations behind this interface; until
then `InMemoryPushDeliveryService` is the default and produces
dry-run results that flow through the audit log + history
endpoint so support engineers can already verify end-to-end
that "send a test push" reaches the delivery seam.

Three types:

    PushPayload            — what to send
    PushDeliveryResult     — what happened
    PushDeliveryService    — ABC; one method `deliver(...)`

Why a dry-run mode at all
-------------------------
Real vendor SDKs hit quotas. Before BE-PUSH-002 lands, we still
need a way to verify "the chain from `POST /push/test` →
device lookup → audit row → ring buffer" works end-to-end.
`dry_run=True` skips the vendor call but still:
  - records the delivery in the in-memory ring buffer
  - emits a JSONL `push.delivery` structlog line
  - returns a synthetic `latency_ms` (5-50ms, deliberately a
    plausible-but-not-real value — see `BE-PUSH-006.md` §3)

When BE-PUSH-002 swaps in a real impl, the public surface
(`deliver(...)` signature + `PushDeliveryResult` shape) stays
the same; only the implementation class changes.

Why a ring buffer instead of an audit-log query for history
-----------------------------------------------------------
Ring buffer (max 100) is process-local and *zero latency*.
The audit log is durable + per-parent + 6-month retention.
A support engineer who's mid-conversation with a parent
needs to confirm "did the test I just fired land?" within
the same request cycle; `GET /push/test/history` answers that
from the ring buffer, not a DB query. The audit log is the
durable trail — different audience, different latency target.
"""

from __future__ import annotations

import random
import threading
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "PushDeliveryService",
    "InMemoryPushDeliveryService",
    "PushPayload",
    "PushDeliveryResult",
    "STATUS_DELIVERED",
    "STATUS_DRY_RUN",
    "STATUS_FAILED",
    "STATUS_SKIPPED_NO_TOKEN",
    "VALID_DELIVERY_STATUSES",
    "DEFAULT_HISTORY_LIMIT",
    "MAX_HISTORY_LIMIT",
    "RING_BUFFER_CAPACITY",
    "get_push_delivery_service",
    "set_push_delivery_service",
    "reset_push_delivery_service_for_tests",
]


# ---------------------------------------------------------------------------
# Status ladder + ring-buffer capacity
# ---------------------------------------------------------------------------

STATUS_DELIVERED: str = "delivered"
STATUS_DRY_RUN: str = "dry_run"
STATUS_FAILED: str = "failed"
STATUS_SKIPPED_NO_TOKEN: str = "skipped_no_token"

VALID_DELIVERY_STATUSES: tuple[str, ...] = (
    STATUS_DELIVERED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_SKIPPED_NO_TOKEN,
)

# Ring buffer holds the most recent 100 deliveries across the whole
# process. Anything older falls off the deque automatically.
RING_BUFFER_CAPACITY: int = 100

# Defaults the history endpoint applies + caps at.
DEFAULT_HISTORY_LIMIT: int = 20
MAX_HISTORY_LIMIT: int = 100

# Synthetic latency for dry-run deliveries. Deliberately a jittered
# 5-50ms — see `BE-PUSH-006.md` §3 for why.
_DRY_RUN_LATENCY_MIN_MS: int = 5
_DRY_RUN_LATENCY_MAX_MS: int = 50


def _device_attr(device: Any, name: str) -> Any:
    """Read `name` from `device` regardless of dict-or-object shape.

    The endpoint passes an `InMemoryPushDeviceRepository` row (a
    plain dict); BE-PUSH-002 will pass a SQLAlchemy `PushDevice`
    ORM instance. Both must work without coupling this module to
    either storage layer.
    """
    if device is None:
        return None
    if isinstance(device, dict):
        return device.get(name)
    return getattr(device, name, None)


# ---------------------------------------------------------------------------
# Pydantic models — these are plain BaseModel subclasses (not pushed
# to the wire as request bodies here) so a future BE-PUSH-002 service
# can construct them without a FastAPI dependency.
# ---------------------------------------------------------------------------


class PushPayload:
    """What to send to a device.

    Fields:
        title:       Notification title (mapped to vendor `title`).
        body:        Notification body (mapped to vendor `body`).
        severity:    One of "info" | "warning" | "critical". Vendor-
                     specific adapters may downgrade or reorder.
        dedupe_key:  Optional key the vendor uses to collapse
                     duplicate deliveries (e.g. alert id).
        data:        Free-form JSON-safe dict; deep-copied on
                     construction so callers can't mutate post-hoc.
    """

    __slots__ = ("title", "body", "severity", "dedupe_key", "data")

    def __init__(
        self,
        *,
        title: str,
        body: str,
        severity: str = "info",
        dedupe_key: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        self.title = title
        self.body = body
        self.severity = severity
        self.dedupe_key = dedupe_key
        # Deep-copy so the caller can't mutate the dict after we
        # record it in the ring buffer.
        self.data: dict[str, Any] = dict(data) if data else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "dedupe_key": self.dedupe_key,
            "data": dict(self.data),
        }


class PushDeliveryResult:
    """What `deliver()` actually did.

    `status` and `dry_run` carry different information:
      - `dry_run` reflects the *caller's request* (echoes back the
        `dry_run` argument).
      - `status` is what actually happened. BE-PUSH-002 will set
        `dry_run=True, status="skipped_no_token"` when the device
        row exists but `push_token` is null — a request for
        dry-run that couldn't go anywhere.
    """

    __slots__ = (
        "device_id",
        "channel",
        "status",
        "error",
        "latency_ms",
        "dry_run",
        "created_at",
    )

    def __init__(
        self,
        *,
        device_id: str,
        channel: str,
        status: str,
        error: Optional[str] = None,
        latency_ms: Optional[int] = None,
        dry_run: bool = False,
        created_at: Optional[datetime] = None,
    ) -> None:
        if status not in VALID_DELIVERY_STATUSES:
            raise ValueError(
                f"invalid status {status!r}; "
                f"expected one of {sorted(VALID_DELIVERY_STATUSES)}"
            )
        self.device_id = device_id
        self.channel = channel
        self.status = status
        self.error = error
        self.latency_ms = latency_ms
        self.dry_run = dry_run
        self.created_at = created_at or datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "channel": self.channel,
            "status": self.status,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "dry_run": self.dry_run,
            "created_at": self.created_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Service ABC
# ---------------------------------------------------------------------------


class PushDeliveryService(ABC):
    """Single seam between Niki and push-vendor SDKs.

    Implementations must be safe to call concurrently from
    multiple request handlers (the singleton is process-wide).
    """

    @abstractmethod
    async def deliver(
        self,
        device: Any,
        payload: PushPayload,
        *,
        dry_run: bool = False,
    ) -> PushDeliveryResult:
        """Deliver `payload` to `device`.

        `device` is typed as `Any` to keep this module decoupled
        from the SQLAlchemy model. The default in-memory impl
        reads only `.device_id` and `.channel`; the real vendor
        impl will read `.push_token` and `.platform`.
        """


# ---------------------------------------------------------------------------
# Default in-memory implementation
# ---------------------------------------------------------------------------


class InMemoryPushDeliveryService(PushDeliveryService):
    """Dry-run + ring-buffer backed implementation (BE-PUSH-006).

    Used in development, tests, and BE-PUSH-006's `POST /push/test`
    until BE-PUSH-002/003 plug in a real vendor SDK.

    Thread-safe: the ring buffer mutation is guarded by a lock;
    reading `history()` returns a snapshot.
    """

    def __init__(self, *, capacity: int = RING_BUFFER_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        # deque(maxlen=N) is the canonical Python ring buffer.
        self._buffer: deque[PushDeliveryResult] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    async def deliver(
        self,
        device: Any,
        payload: PushPayload,
        *,
        dry_run: bool = False,
    ) -> PushDeliveryResult:
        device_id = _device_attr(device, "device_id") or "unknown"
        channel = _device_attr(device, "channel") or "unknown"
        push_token = _device_attr(device, "push_token") or ""

        # No token at all — there's nothing to send. This path is
        # exercised when /push/register was called without a token
        # (e.g. during Web Push provisioning before the user
        # granted permission).
        if not push_token:
            result = PushDeliveryResult(
                device_id=device_id,
                channel=channel,
                status=STATUS_SKIPPED_NO_TOKEN,
                error="device has no push_token",
                latency_ms=None,
                dry_run=dry_run,
            )
        elif dry_run:
            latency = random.randint(
                _DRY_RUN_LATENCY_MIN_MS, _DRY_RUN_LATENCY_MAX_MS
            )
            result = PushDeliveryResult(
                device_id=device_id,
                channel=channel,
                status=STATUS_DRY_RUN,
                error=None,
                latency_ms=latency,
                dry_run=True,
            )
        else:
            # A non-dry-run call to the in-memory stub is treated
            # as a *failure* (we have no vendor SDK). BE-PUSH-002
            # will replace this branch with a real vendor call.
            result = PushDeliveryResult(
                device_id=device_id,
                channel=channel,
                status=STATUS_FAILED,
                error="in-memory stub cannot deliver to a vendor",
                latency_ms=None,
                dry_run=False,
            )

        # Record + log. Both happen *after* status is decided so
        # the ring buffer never sees a partially-built result.
        self._record(result, payload=payload)
        logger.info(
            "push.delivery",
            device_id=result.device_id,
            channel=result.channel,
            status=result.status,
            dry_run=result.dry_run,
            latency_ms=result.latency_ms,
            error=result.error,
            title=payload.title,
            severity=payload.severity,
            dedupe_key=payload.dedupe_key,
        )
        return result

    # -- history ---------------------------------------------------------

    def history(
        self,
        *,
        limit: int = DEFAULT_HISTORY_LIMIT,
        channel: Optional[str] = None,
    ) -> list[PushDeliveryResult]:
        """Return a snapshot of recent deliveries, newest first.

        `limit` is clamped to [1, MAX_HISTORY_LIMIT]. The buffer
        is itself bounded to RING_BUFFER_CAPACITY so the cap on
        `limit` is really a *floor* — there's nothing older to
        return anyway.
        """
        if limit <= 0:
            limit = DEFAULT_HISTORY_LIMIT
        if limit > MAX_HISTORY_LIMIT:
            limit = MAX_HISTORY_LIMIT

        with self._lock:
            snapshot = list(self._buffer)
        # The deque was filled oldest-first; reverse for newest-first.
        snapshot.reverse()

        if channel is not None:
            snapshot = [r for r in snapshot if r.channel == channel]
        return snapshot[:limit]

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)

    def clear(self) -> None:
        """Drop every recorded delivery. Test-only helper."""
        with self._lock:
            self._buffer.clear()

    # -- internals -------------------------------------------------------

    def _record(
        self,
        result: PushDeliveryResult,
        *,
        payload: PushPayload,
    ) -> None:
        with self._lock:
            self._buffer.append(result)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_service: Optional[PushDeliveryService] = None
_service_lock = threading.Lock()


def get_push_delivery_service() -> PushDeliveryService:
    """Return the process-wide `PushDeliveryService`.

    Lazy-builds an `InMemoryPushDeliveryService` on first access.
    Safe under concurrent first-access (uses a real lock so two
    racing callers can't end up with two instances).
    """
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = InMemoryPushDeliveryService()
    return _service


def set_push_delivery_service(service: PushDeliveryService) -> None:
    """Override the process-wide service (tests + ops tools).

    Pass `None` to clear the cache so the next `get_*` rebuilds
    a fresh default — useful for tests that want to start from
    a clean ring buffer without re-instantiating explicitly.
    """
    global _service
    _service = service


def reset_push_delivery_service_for_tests() -> None:
    """Drop the cached singleton so the next `get_*` rebuilds it.

    Test-only helper. Production code should never call this.
    """
    global _service
    _service = None