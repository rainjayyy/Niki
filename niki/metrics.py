"""Prometheus metrics registry (BE-API-009).

Exposes a process-wide singleton :class:`MetricsRegistry` that owns a
custom ``CollectorRegistry`` (NOT ``prometheus_client.REGISTRY``) and
pre-registers the 13 Niki metrics enumerated in the ticket:

    - CHAT_TOTAL                (Counter)  — every chat request
    - SAFETY_BLOCK_TOTAL        (Counter)  — every safety block / escalation
    - HIGH_RISK_TOTAL           (Counter)  — high-risk alerts
    - SMS_SENT_TOTAL            (Counter)  — SMS dispatch outcomes
    - LOGIN_SUCCESS_TOTAL       (Counter)  — successful logins
    - LOGIN_FAILURE_TOTAL       (Counter)  — failed logins
    - PUSH_DELIVERY_TOTAL       (Counter)  — push delivery outcomes
    - OCR_REQUESTS_TOTAL        (Counter)  — OCR request outcomes
    - RATE_LIMITED_TOTAL        (Counter)  — 429 responses emitted
    - WEBHOOK_DELIVERIES_TOTAL  (Counter)  — webhook deliveries
    - AUTH_AUDIT_TOTAL          (Counter)  — auth audit events
    - HTTP_REQUEST_DURATION     (Histogram)— request latency by (method, endpoint)
    - ACTIVE_PARENTS            (Gauge)    — currently active parents (v1: 0)

Usage in hot paths:

    from niki.metrics import get_registry

    get_registry().CHAT_TOTAL.labels(
        route="chat_stream", provider="mock", mode="stream"
    ).inc()

Design notes:

- **Custom registry.** Tests run inside a single Python process and
  pytest fixtures reset state between tests. If we registered against
  ``prometheus_client.REGISTRY`` (the default process-global), counter
  values would leak across tests and across pytest workers. Instead,
  we keep our own ``CollectorRegistry`` behind a ``reset_for_tests()``
  helper so each test gets a fresh metric set starting from zero.
- **Hot paths look up metrics lazily.** Callers must use
  ``get_registry().METRIC_NAME`` rather than caching a direct
  reference to the counter. After ``reset_for_tests()`` the cache is
  dropped and the next lookup returns a fresh metric bound to the
  fresh registry.
- **Cardinality guards.** Labels are limited to known-bounded sets
  (route templates, provider names, alert types). The HTTP middleware
  uses ``request.scope['route'].path`` (the FastAPI route template)
  rather than the raw URL path so user-supplied values like phone
  numbers or UUIDs never become label values.
"""

from __future__ import annotations

import threading

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)


class MetricsRegistry:
    """Owns a single ``CollectorRegistry`` and all Niki metrics.

    Created by :func:`get_registry` — one instance per process, cached.
    :func:`reset_for_tests` drops the cache and rebuilds from scratch
    on the next call.
    """

    def __init__(self) -> None:
        # CRITICAL: do not use ``prometheus_client.REGISTRY``. We want
        # test isolation and process-scoped metrics; the default
        # registry accumulates state for the whole Python process.
        self.registry: CollectorRegistry = CollectorRegistry()

        # ------------------------------------------------------------------
        # Counters
        # ------------------------------------------------------------------
        self.CHAT_TOTAL: Counter = Counter(
            "niki_chat_total",
            "Total chat requests served.",
            ["route", "provider", "mode"],
            registry=self.registry,
        )
        self.SAFETY_BLOCK_TOTAL: Counter = Counter(
            "niki_safety_block_total",
            "Total safety blocks and escalations.",
            ["reason", "severity"],
            registry=self.registry,
        )
        self.HIGH_RISK_TOTAL: Counter = Counter(
            "niki_high_risk_total",
            "Total high-risk alert events recorded.",
            ["alert_type"],
            registry=self.registry,
        )
        self.SMS_SENT_TOTAL: Counter = Counter(
            "niki_sms_sent_total",
            "Total SMS messages dispatched.",
            ["provider", "status"],
            registry=self.registry,
        )
        self.LOGIN_SUCCESS_TOTAL: Counter = Counter(
            "niki_login_success_total",
            "Total successful parent logins.",
            ["method"],
            registry=self.registry,
        )
        self.LOGIN_FAILURE_TOTAL: Counter = Counter(
            "niki_login_failure_total",
            "Total failed parent logins.",
            ["method", "reason"],
            registry=self.registry,
        )
        self.PUSH_DELIVERY_TOTAL: Counter = Counter(
            "niki_push_delivery_total",
            "Total push notification deliveries.",
            ["channel", "status"],
            registry=self.registry,
        )
        self.OCR_REQUESTS_TOTAL: Counter = Counter(
            "niki_ocr_requests_total",
            "Total OCR requests processed.",
            ["status"],
            registry=self.registry,
        )
        self.RATE_LIMITED_TOTAL: Counter = Counter(
            "niki_rate_limited_total",
            "Total responses with HTTP 429 emitted.",
            ["endpoint"],
            registry=self.registry,
        )
        self.WEBHOOK_DELIVERIES_TOTAL: Counter = Counter(
            "niki_webhook_deliveries_total",
            "Total webhook deliveries.",
            ["event_type", "status"],
            registry=self.registry,
        )
        self.AUTH_AUDIT_TOTAL: Counter = Counter(
            "niki_auth_audit_total",
            "Auth audit events recorded.",
            ["event_type", "severity"],
            registry=self.registry,
        )

        # ------------------------------------------------------------------
        # Histogram — HTTP request latency
        # ------------------------------------------------------------------
        self.HTTP_REQUEST_DURATION: Histogram = Histogram(
            "niki_http_request_duration_seconds",
            "HTTP request latency in seconds.",
            ["method", "endpoint"],
            # Buckets tuned for typical Niki API latencies (sub-second
            # for chat/safety, 2-3s for heavy LLM paths).
            buckets=(
                0.005,
                0.01,
                0.025,
                0.05,
                0.1,
                0.25,
                0.5,
                1.0,
                2.5,
                5.0,
                10.0,
            ),
            registry=self.registry,
        )

        # ------------------------------------------------------------------
        # Gauge — active parents (placeholder; left at 0 for v1)
        # ------------------------------------------------------------------
        self.ACTIVE_PARENTS: Gauge = Gauge(
            "niki_active_parents",
            "Currently active parents (rolling 5-min window).",
            registry=self.registry,
        )
        # Leave at 0 — the BE-REALTIME tickets will populate this from
        # the heartbeat service once it ships.

        # ------------------------------------------------------------------
        # North Star metric — emotion recovery latency
        # (BE-ISS-NORTHSTAR: the resume claim "以「主动关怀后情绪
        # 恢复至基线时间」替代「情绪识别准确率」作为系统北极星
        # 指标" is wired to this histogram + counter pair.)
        # ------------------------------------------------------------------
        self.EMOTION_ANOMALIES_TOTAL: Counter = Counter(
            "niki_emotion_anomalies_total",
            "Total per-child emotion anomalies opened (|z|>1.5).",
            ["direction"],
            registry=self.registry,
        )
        self.EMOTION_RECOVERY_SECONDS: Histogram = Histogram(
            "niki_emotion_recovery_seconds",
            (
                "Time elapsed between an emotion anomaly opening and "
                "the first subsequent in-baseline record. The North "
                "Star metric for the proactive-care feature."
            ),
            # Tuned for daily-granularity recovery: 1m..14d.
            buckets=(
                60.0,  # 1 minute
                300.0,  # 5 minutes
                1800.0,  # 30 minutes
                3600.0,  # 1 hour
                21600.0,  # 6 hours
                86400.0,  # 1 day
                259200.0,  # 3 days
                604800.0,  # 7 days
            ),
            registry=self.registry,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: MetricsRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> MetricsRegistry:
    """Return the process-wide :class:`MetricsRegistry` (lazy-built).

    The registry is built once per process and cached.
    :func:`reset_for_tests` drops the cache so the next call rebuilds
    from scratch. Hot-path code MUST use this getter rather than
    caching direct references to counter objects.
    """
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = MetricsRegistry()
        return _registry


def reset_for_tests() -> None:
    """Drop the cached registry so the next ``get_registry()`` rebuilds.

    Called between pytest tests (and never in production) to guarantee
    counter / histogram state does not leak across test boundaries.
    """
    global _registry
    with _registry_lock:
        _registry = None


__all__ = [
    "MetricsRegistry",
    "get_registry",
    "reset_for_tests",
]
