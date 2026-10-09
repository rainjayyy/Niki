"""Push delivery abstraction (BE-PUSH-006 + BE-PUSH-003).

Single seam between Niki and any push-vendor SDK (HMS / MiPush /
OPPO / Vivo / Flyme / APNs / FCM / Web Push). BE-PUSH-003 filled
in the Web Push half (`WebPushDeliveryBackend`, real deliveries via
VAPID); BE-PUSH-002 will do the same for the mobile vendors. Until
then every non-web platform still lands on
`InMemoryPushDeliveryService`'s dry-run result, which flows through
the audit log + history endpoint so support engineers can already
verify end-to-end that "send a test push" reaches the delivery seam.

Five types:

    PushPayload                   — what to send
    PushDeliveryResult            — what happened
    PushDeliveryService           — ABC; one method `deliver(...)`
    WebPushDeliveryBackend        — real Web Push (RFC 8030 + VAPID)
    CompositePushDeliveryService  — per-platform dispatch + history

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

import asyncio
import functools
import json
import random
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "PushDeliveryService",
    "InMemoryPushDeliveryService",
    "WebPushDeliveryBackend",
    "CompositePushDeliveryService",
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
    "EVENT_WEB_PUSH_DELIVERED",
    "EVENT_WEB_PUSH_DRY_RUN",
    "EVENT_WEB_PUSH_FAILED",
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
                f"invalid status {status!r}; expected one of {sorted(VALID_DELIVERY_STATUSES)}"
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
            latency = random.randint(_DRY_RUN_LATENCY_MIN_MS, _DRY_RUN_LATENCY_MAX_MS)
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
# BE-PUSH-003 — real Web Push (RFC 8030) delivery behind the same seam
# ---------------------------------------------------------------------------

# Audit event types. Free-form strings by design (see
# `niki.db.models.audit_log`), kept here so the backend and its tests
# can't spell them differently.
EVENT_WEB_PUSH_DELIVERED: str = "web_push_delivered"
EVENT_WEB_PUSH_FAILED: str = "web_push_failed"
EVENT_WEB_PUSH_DRY_RUN: str = "web_push_dry_run"

# Dry-run latency for the *web* backend. Slightly higher floor than
# the in-memory stub's 5ms: a real Web Push round-trip to FCM/Mozilla
# is never faster than ~10ms, and a plausible number keeps ops
# dashboards from learning a fantasy baseline.
_WEB_DRY_RUN_LATENCY_MIN_MS: int = 10
_WEB_DRY_RUN_LATENCY_MAX_MS: int = 50


def _default_webpush_send(**kwargs: Any) -> Any:
    """Call `pywebpush.webpush(**kwargs)`, importing it lazily.

    Lazy because `pywebpush` is an optional runtime dependency: the
    module must import (and every non-web code path must work) on a
    box where it isn't installed. Tests monkeypatch this function or
    inject `WebPushDeliveryBackend(webpush_fn=...)`.
    """
    from pywebpush import webpush  # noqa: PLC0415 — deliberate lazy import

    return webpush(**kwargs)


def _webpush_exception_type() -> type[BaseException]:
    """Return `pywebpush.WebPushException`, or a never-raised stand-in.

    Lets `except _webpush_exception_type()` stay valid when the
    library is absent — we then fall through to the generic
    `Exception` handler, which produces the same `failed` result.
    """
    try:
        from pywebpush import WebPushException  # noqa: PLC0415
    except Exception:  # pragma: no cover — import-error path

        class _NeverRaised(Exception):
            pass

        return _NeverRaised
    return WebPushException


def _response_status(obj: Any) -> Optional[int]:
    """Pull an HTTP status code off a response *or* an exception."""
    code = getattr(obj, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(obj, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _endpoint_host(endpoint: str) -> Optional[str]:
    """Host of a push endpoint — never the path.

    The path segment of a Web Push endpoint *is* the subscription
    credential: anyone holding it can push to that browser. It must
    never reach the audit table, which support engineers (and, via
    /admin/audit, parents) can read. The host alone still answers the
    only operational question we ask of it — "which push service is
    flaking?".
    """
    try:
        return urlparse(endpoint).netloc or None
    except ValueError:  # pragma: no cover — urlparse is very forgiving
        return None


class WebPushDeliveryBackend(PushDeliveryService):
    """Deliver a payload to a browser via Web Push + VAPID.

    Handles only `platform == "web"` devices; anything else comes
    back `skipped_no_token` so a caller that hands us the wrong
    device gets a *result*, not an exception (delivery is a
    best-effort side channel — it must never 500 the request that
    triggered it).

    The `push_token` column carries the subscription in one of two
    shapes, both accepted:

      - a bare endpoint URL, with `p256dh` / `auth` read off the
        device row if present;
      - the full `PushSubscription.toJSON()` string, which is what
        the browser hands the frontend verbatim.

    `pywebpush` is synchronous (it uses `requests`), so the actual
    send runs in a worker thread — otherwise one slow push service
    would stall the whole event loop.
    """

    def __init__(
        self,
        *,
        webpush_fn: Optional[Callable[..., Any]] = None,
        settings: Any = None,
        audit_service: Any = None,
    ) -> None:
        self._webpush_fn = webpush_fn
        self._settings = settings
        self._audit_service = audit_service

    # -- collaborators (resolved late so tests can swap singletons) ------

    def _get_settings(self) -> Any:
        if self._settings is not None:
            return self._settings
        from niki.config.settings import get_settings

        return get_settings()

    def _get_audit_service(self) -> Any:
        if self._audit_service is not None:
            return self._audit_service
        from niki.services.audit_log import get_audit_log_service

        return get_audit_log_service()

    # -- main entry point ------------------------------------------------

    async def deliver(
        self,
        device: Any,
        payload: PushPayload,
        *,
        dry_run: bool = False,
    ) -> PushDeliveryResult:
        device_id = _device_attr(device, "device_id") or "unknown"
        channel = _device_attr(device, "channel") or "web"
        platform = _device_attr(device, "platform")
        push_token = _device_attr(device, "push_token") or ""
        parent_id = _device_attr(device, "parent_id")

        # 1. Not our platform. Cheapest check first.
        if platform != "web":
            return await self._finish(
                device_id=device_id,
                channel=channel,
                parent_id=parent_id,
                status=STATUS_SKIPPED_NO_TOKEN,
                error=f"platform {platform!r} is not web",
                dry_run=dry_run,
                endpoint=None,
                status_code=None,
                latency_ms=None,
            )

        # 2. Token must parse into a subscription with an https endpoint.
        subscription = _parse_subscription(push_token)
        if subscription is None:
            return await self._finish(
                device_id=device_id,
                channel=channel,
                parent_id=parent_id,
                status=STATUS_SKIPPED_NO_TOKEN,
                error="device has no usable web push subscription",
                dry_run=dry_run,
                endpoint=None,
                status_code=None,
                latency_ms=None,
            )
        _attach_device_keys(subscription, device)
        endpoint = subscription["endpoint"]

        # 3. Probe mode — walk everything except the network call.
        if dry_run:
            return await self._finish(
                device_id=device_id,
                channel=channel,
                parent_id=parent_id,
                status=STATUS_DRY_RUN,
                error=None,
                dry_run=True,
                endpoint=endpoint,
                status_code=None,
                latency_ms=random.randint(
                    _WEB_DRY_RUN_LATENCY_MIN_MS,
                    _WEB_DRY_RUN_LATENCY_MAX_MS,
                ),
            )

        settings = self._get_settings()
        try:
            from niki.config.settings import get_or_generate_vapid_keys

            private_key, _public_key = get_or_generate_vapid_keys(settings)
        except Exception as exc:  # key material unavailable
            return await self._finish(
                device_id=device_id,
                channel=channel,
                parent_id=parent_id,
                status=STATUS_FAILED,
                error=f"VAPID_KEY_UNAVAILABLE: {exc}",
                dry_run=False,
                endpoint=endpoint,
                status_code=None,
                latency_ms=None,
            )

        ttl = int(getattr(settings, "web_push_ttl_seconds", 86400) or 86400)
        urgency = str(getattr(settings, "web_push_urgency", "normal") or "normal")
        headers: dict[str, str] = {"TTL": str(ttl), "Urgency": urgency}
        if payload.dedupe_key:
            # RFC 8030 §5.4: a push service collapses undelivered
            # messages that share a Topic — exactly our dedupe
            # semantics, for free, on the push service's side.
            headers["Topic"] = payload.dedupe_key[:32]

        send_kwargs: dict[str, Any] = {
            "subscription_info": subscription,
            "data": json.dumps(payload.to_dict(), ensure_ascii=False),
            "vapid_private_key": private_key,
            "vapid_claims": {"sub": settings.vapid_subject},
            "ttl": ttl,
            "headers": headers,
        }

        started = time.monotonic()
        status_code: Optional[int] = None
        error: Optional[str] = None
        try:
            response = await self._send(send_kwargs)
            status_code = _response_status(response)
        except _webpush_exception_type() as exc:
            status_code = _response_status(exc)
            error = f"WEB_PUSH_ERROR: {exc}"
        except ImportError as exc:
            status_code = None
            error = f"PYWEBPUSH_UNAVAILABLE: {exc}"
        except Exception as exc:  # network / TLS / DNS
            status_code = None
            error = f"NETWORK_ERROR: {exc}"
        latency_ms = int((time.monotonic() - started) * 1000)

        # A push service answers 201 Created on success; 200/202 are
        # accepted by some implementations, so treat any 2xx as sent.
        delivered = error is None and (status_code is None or 200 <= status_code < 300)
        if not delivered and error is None:
            error = f"HTTP_{status_code}"

        return await self._finish(
            device_id=device_id,
            channel=channel,
            parent_id=parent_id,
            status=STATUS_DELIVERED if delivered else STATUS_FAILED,
            error=error,
            dry_run=False,
            endpoint=endpoint,
            status_code=status_code,
            latency_ms=latency_ms,
        )

    # -- internals -------------------------------------------------------

    async def _send(self, send_kwargs: dict[str, Any]) -> Any:
        """Run the (synchronous) pywebpush call off the event loop."""
        fn = self._webpush_fn or _default_webpush_send
        return await asyncio.to_thread(functools.partial(fn, **send_kwargs))

    async def _finish(
        self,
        *,
        device_id: str,
        channel: str,
        parent_id: Optional[str],
        status: str,
        error: Optional[str],
        dry_run: bool,
        endpoint: Optional[str],
        status_code: Optional[int],
        latency_ms: Optional[int],
    ) -> PushDeliveryResult:
        """Build the result, write the audit row, emit the log line.

        Every exit path from `deliver()` goes through here, which is
        what makes AC-6 ("audit log on *every* delivery attempt")
        true by construction rather than by discipline.
        """
        result = PushDeliveryResult(
            device_id=device_id,
            channel=channel,
            status=status,
            error=error,
            latency_ms=latency_ms,
            dry_run=dry_run,
        )
        await self._audit(
            result,
            parent_id=parent_id,
            endpoint=endpoint,
            status_code=status_code,
        )
        logger.info(
            "push.web_delivery",
            device_id=device_id,
            channel=channel,
            status=status,
            dry_run=dry_run,
            latency_ms=latency_ms,
            status_code=status_code,
            endpoint_host=_endpoint_host(endpoint) if endpoint else None,
            error=error,
        )
        return result

    async def _audit(
        self,
        result: PushDeliveryResult,
        *,
        parent_id: Optional[str],
        endpoint: Optional[str],
        status_code: Optional[int],
    ) -> None:
        if result.status == STATUS_DELIVERED:
            event_type, severity = EVENT_WEB_PUSH_DELIVERED, "info"
        elif result.status == STATUS_DRY_RUN:
            event_type, severity = EVENT_WEB_PUSH_DRY_RUN, "info"
        else:
            # `skipped_no_token` is a misconfiguration, not a crisis,
            # but it still deserves the warning ladder: a parent who
            # never gets alerts wants it visible somewhere.
            event_type, severity = EVENT_WEB_PUSH_FAILED, "warning"

        try:
            await self._get_audit_service().log_event(
                event_type=event_type,
                severity=severity,
                actor_parent_id=parent_id,
                target_parent_id=parent_id,
                endpoint="/api/v1/push/web",
                method="POST",
                reason="BE-PUSH-003 web push delivery attempt",
                metadata={
                    "subscription_id": result.device_id,
                    # Host only — see `_endpoint_host`.
                    "endpoint": _endpoint_host(endpoint) if endpoint else None,
                    "status_code": status_code,
                    "latency_ms": result.latency_ms,
                    "delivery_status": result.status,
                },
            )
        except Exception as exc:  # pragma: no cover — audit must not raise
            logger.warning("push.web_audit_failed", error=str(exc))


def _parse_subscription(push_token: str) -> Optional[dict[str, Any]]:
    """Coerce a stored `push_token` into a pywebpush subscription dict.

    Returns None when the token can't possibly be a Web Push
    subscription — an empty string, a vendor token from another
    platform, or an http:// endpoint (push services are https-only,
    and accepting http would mean shipping the payload in clear).
    """
    token = (push_token or "").strip()
    if not token:
        return None

    if token.startswith("{"):
        try:
            parsed = json.loads(token)
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        endpoint = parsed.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            return None
        keys = parsed.get("keys")
        subscription: dict[str, Any] = {"endpoint": endpoint}
        if isinstance(keys, dict):
            subscription["keys"] = dict(keys)
        return subscription

    if not token.startswith("https://"):
        return None
    return {"endpoint": token}


def _attach_device_keys(subscription: dict[str, Any], device: Any) -> None:
    """Fill `keys` from the device row when the token was endpoint-only.

    Both `p256dh` and `auth` are required for payload encryption; a
    half-filled pair is worse than none (pywebpush would raise), so
    we only attach when both are present.
    """
    if subscription.get("keys"):
        return
    p256dh = _device_attr(device, "p256dh") or _device_attr(device, "push_p256dh")
    auth = _device_attr(device, "auth") or _device_attr(device, "push_auth")
    if p256dh and auth:
        subscription["keys"] = {"p256dh": p256dh, "auth": auth}


class CompositePushDeliveryService(InMemoryPushDeliveryService):
    """Route each device to the backend that can actually reach it.

    Today that's a two-way split:

        platform == "web"  → `WebPushDeliveryBackend` (real delivery)
        everything else    → the inherited dry-run stub

    It *inherits* from `InMemoryPushDeliveryService` rather than
    wrapping it so both branches land in the same ring buffer —
    `GET /push/test/history` is the support engineer's single pane of
    glass and would be half-blind otherwise.

    BE-PUSH-002 adds APNs / FCM / HMS branches here; the dispatch
    table is the only thing that changes.
    """

    def __init__(
        self,
        *,
        capacity: int = RING_BUFFER_CAPACITY,
        web_backend: Optional[PushDeliveryService] = None,
    ) -> None:
        super().__init__(capacity=capacity)
        self._web_backend: PushDeliveryService = web_backend or WebPushDeliveryBackend()

    @property
    def web_backend(self) -> PushDeliveryService:
        return self._web_backend

    async def deliver(
        self,
        device: Any,
        payload: PushPayload,
        *,
        dry_run: bool = False,
    ) -> PushDeliveryResult:
        if _device_attr(device, "platform") != "web":
            return await super().deliver(device, payload, dry_run=dry_run)

        result = await self._web_backend.deliver(device, payload, dry_run=dry_run)
        # The web backend does its own audit + logging; the ring
        # buffer is this class's job.
        self._record(result, payload=payload)
        return result


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_service: Optional[PushDeliveryService] = None
_service_lock = threading.Lock()


def get_push_delivery_service() -> PushDeliveryService:
    """Return the process-wide `PushDeliveryService`.

    Lazy-builds a `CompositePushDeliveryService` on first access
    (BE-PUSH-003: real Web Push for browsers, dry-run stub for
    every other platform). Safe under concurrent first-access
    (uses a real lock so two racing callers can't end up with two
    instances).
    """
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = CompositePushDeliveryService()
    return _service


def set_push_delivery_service(service: PushDeliveryService) -> None:
    """Override the process-wide service (tests + ops tools).

    Pass `None` to clear the cache so the next `get_*` rebuilds
    the default (`CompositePushDeliveryService`) — useful for tests
    that want to start from a clean ring buffer without
    re-instantiating explicitly.
    """
    global _service
    _service = service


def reset_push_delivery_service_for_tests() -> None:
    """Drop the cached singleton so the next `get_*` rebuilds it.

    Test-only helper. Production code should never call this.
    """
    global _service
    _service = None
