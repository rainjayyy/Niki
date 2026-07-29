"""Security alert dispatch (BE-AUTH-001-rollout-alerts).

Defines `SecurityAlert` + `AlertDispatcher` ABC + three concrete
sinks:

    - ConsoleAlertDispatcher   — structlog.critical (default)
    - WebhookAlertDispatcher   — httpx POST to a configured URL
    - CompositeAlertDispatcher — fan-out to multiple sinks

All sinks are fail-safe: a dispatcher error never raises into the
caller (the caller has already returned 4xx to the user). Errors
are logged at `error` level for ops to investigate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Alert dataclass
# ---------------------------------------------------------------------------


SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
_VALID_SEVERITIES = frozenset({SEVERITY_WARNING, SEVERITY_CRITICAL})


@dataclass(frozen=True)
class SecurityAlert:
    """A single alert to dispatch to ops / IM / on-call."""

    alert_type: str
    severity: str
    actor_parent_id: str | None
    target_parent_id: str | None
    target_child_id: str | None
    client_ip: str | None
    event_count: int
    window_seconds: int
    detected_at: datetime
    title: str
    detail: str

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"invalid severity {self.severity!r}; "
                f"expected one of {sorted(_VALID_SEVERITIES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_type": self.alert_type,
            "severity": self.severity,
            "actor_parent_id": self.actor_parent_id,
            "target_parent_id": self.target_parent_id,
            "target_child_id": self.target_child_id,
            "client_ip": self.client_ip,
            "event_count": self.event_count,
            "window_seconds": self.window_seconds,
            "detected_at": self.detected_at.isoformat(),
            "title": self.title,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Dispatcher contract
# ---------------------------------------------------------------------------


class AlertDispatcher(ABC):
    """Where SecurityAlerts go. Implementations must NEVER raise."""

    @abstractmethod
    async def dispatch(self, alert: SecurityAlert) -> None: ...


# ---------------------------------------------------------------------------
# Concrete dispatchers
# ---------------------------------------------------------------------------


class ConsoleAlertDispatcher(AlertDispatcher):
    """Logs the alert at `critical` level.

    This is the default sink — even when other dispatchers are
    configured, this should usually run alongside (composite) so
    the alert is visible in stdout / ELK / Loki.
    """

    async def dispatch(self, alert: SecurityAlert) -> None:
        log_fn = (
            logger.critical
            if alert.severity == SEVERITY_CRITICAL
            else logger.warning
        )
        log_fn(
            f"security.alert.{alert.alert_type}",
            **alert.to_dict(),
        )


class WebhookAlertDispatcher(AlertDispatcher):
    """POST the alert as JSON to a configured webhook URL.

    Suitable for Feishu / Lark / Slack / custom on-call endpoints.
    HTTP failures are logged but never raised.
    """

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not url:
            raise ValueError(
                "WebhookAlertDispatcher requires a non-empty url"
            )
        self._url = url
        self._timeout = timeout_seconds
        self._client = client  # injectable for tests

    async def dispatch(self, alert: SecurityAlert) -> None:
        payload = alert.to_dict()
        try:
            client = self._client or httpx.AsyncClient(
                timeout=self._timeout,
            )
            try:
                resp = await client.post(
                    self._url, json=payload
                )
                if resp.status_code >= 400:
                    logger.error(
                        "security.alert.webhook_non_2xx",
                        url=self._url,
                        status=resp.status_code,
                        alert_type=alert.alert_type,
                    )
            finally:
                if self._client is None:
                    await client.aclose()
        except Exception as exc:  # pragma: no cover — network path
            logger.error(
                "security.alert.webhook_failed",
                url=self._url,
                error=str(exc),
                alert_type=alert.alert_type,
            )


class CompositeAlertDispatcher(AlertDispatcher):
    """Fan-out: dispatch to every sink, swallow errors per-sink.

    A sink failure does not stop the others from receiving the
    alert (defence in depth — losing one channel shouldn't lose
    the alert).
    """

    def __init__(self, dispatchers: list[AlertDispatcher]) -> None:
        if not dispatchers:
            raise ValueError(
                "CompositeAlertDispatcher needs at least one sink"
            )
        self._dispatchers = list(dispatchers)

    async def dispatch(self, alert: SecurityAlert) -> None:
        for d in self._dispatchers:
            try:
                await d.dispatch(alert)
            except Exception as exc:  # pragma: no cover
                logger.error(
                    "security.alert.sink_failed",
                    sink=type(d).__name__,
                    error=str(exc),
                    alert_type=alert.alert_type,
                )


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_dispatcher: AlertDispatcher | None = None


def get_alert_dispatcher() -> AlertDispatcher:
    """Return the process-wide alert dispatcher (default: console)."""
    global _dispatcher
    if _dispatcher is None:
        sinks: list[AlertDispatcher] = [ConsoleAlertDispatcher()]
        from niki.config.settings import get_settings
        settings = get_settings()
        if settings.auth_alert_webhook_url:
            sinks.append(
                WebhookAlertDispatcher(
                    url=settings.auth_alert_webhook_url,
                    timeout_seconds=settings.auth_alert_webhook_timeout_s,
                )
            )
        if len(sinks) == 1:
            _dispatcher = sinks[0]
        else:
            _dispatcher = CompositeAlertDispatcher(sinks)
    return _dispatcher


def set_alert_dispatcher(dispatcher: AlertDispatcher) -> None:
    """Override the process-wide alert dispatcher (tests)."""
    global _dispatcher
    _dispatcher = dispatcher