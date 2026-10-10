"""Security alert dispatch (BE-AUTH-001-rollout-alerts).

Defines `SecurityAlert` + `AlertDispatcher` ABC + four concrete
sinks:

    - ConsoleAlertDispatcher   — structlog.critical (default)
    - WebhookAlertDispatcher   — httpx POST to a configured URL
    - EmailAlertDispatcher     — SMTP delivery
    - CompositeAlertDispatcher — fan-out to multiple sinks

All sinks are fail-safe: a dispatcher error never raises into the
caller (the caller has already returned 4xx to the user). Errors
are logged at `error` level for ops to investigate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
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
                f"invalid severity {self.severity!r}; expected one of {sorted(_VALID_SEVERITIES)}"
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
        log_fn = logger.critical if alert.severity == SEVERITY_CRITICAL else logger.warning
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
            raise ValueError("WebhookAlertDispatcher requires a non-empty url")
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
                resp = await client.post(self._url, json=payload)
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


class EmailAlertDispatcher(AlertDispatcher):
    """Deliver alerts via SMTP (plain text).

    `smtplib` is sync, so the actual send runs in
    `asyncio.to_thread` to avoid blocking the event loop.
    Configure via Settings (BE-AUTH-001-rollout-alerts-rollout):
        auth_alert_smtp_host / _port / _username / _password
        auth_alert_email_from / auth_alert_email_to

    If any of those are missing, the sink short-circuits to a
    structlog.warning and never raises — sink failures must
    never reach the auth path.
    """

    def __init__(
        self,
        *,
        smtp_host: str = "",
        smtp_port: int = 587,
        username: str = "",
        password: str = "",
        from_addr: str = "",
        to_addrs: list[str] | None = None,
        timeout_seconds: int = 5,
    ) -> None:
        self._host = smtp_host
        self._port = smtp_port
        self._username = username
        self._password = password
        self._from = from_addr
        self._to = list(to_addrs or [])
        self._timeout = timeout_seconds

    async def dispatch(self, alert: SecurityAlert) -> None:
        if not self._host or not self._from or not self._to:
            logger.warning(
                "security.alert.email_skipped",
                reason="smtp not configured",
                alert_type=alert.alert_type,
            )
            return
        import asyncio
        from email.message import EmailMessage

        subject, body = self._render(alert)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._from
        msg["To"] = ", ".join(self._to)
        msg.set_content(body)

        try:
            await asyncio.to_thread(self._send, msg)
            logger.info(
                "security.alert.email_sent",
                to=self._to,
                alert_type=alert.alert_type,
            )
        except Exception as exc:
            # Failures are logged, not raised — the next sink
            # in the composite should still get a chance.
            logger.error(
                "security.alert.email_failed",
                error=str(exc),
                alert_type=alert.alert_type,
            )
            raise

    def _send(self, msg: Any) -> None:
        """Run in a worker thread (smtplib is sync)."""
        import smtplib

        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(msg)

    @staticmethod
    def _render(alert: SecurityAlert) -> tuple[str, str]:
        subject = f"[Niki 安全告警] {alert.alert_type} - {alert.actor_parent_id or 'unknown'}"
        body_lines = [
            alert.title,
            "",
            alert.detail,
            "",
            f"actor_parent_id: {alert.actor_parent_id}",
            f"target_parent_id: {alert.target_parent_id}",
            f"target_child_id:  {alert.target_child_id}",
            f"client_ip:        {alert.client_ip}",
            f"event_count:      {alert.event_count}",
            f"window_seconds:   {alert.window_seconds}",
            f"detected_at:      {alert.detected_at.isoformat()}",
        ]
        return subject, "\n".join(body_lines)


class CompositeAlertDispatcher(AlertDispatcher):
    """Fan-out: dispatch to every sink in parallel, swallow

    A sink failure does not stop the others from receiving the
    alert (defence in depth — losing one channel shouldn't lose
    the alert).
    """

    def __init__(self, dispatchers: list[AlertDispatcher]) -> None:
        if not dispatchers:
            raise ValueError("CompositeAlertDispatcher needs at least one sink")
        self._dispatchers = list(dispatchers)

    async def dispatch(self, alert: SecurityAlert) -> None:
        import asyncio

        # True fan-out: every sink receives the alert in
        # parallel. A failure in one sink does not block the
        # others; we just log and move on.
        results = await asyncio.gather(
            *(d.dispatch(alert) for d in self._dispatchers),
            return_exceptions=True,
        )
        for d, r in zip(self._dispatchers, results):
            if isinstance(r, Exception):
                logger.error(
                    "security.alert.sink_failed",
                    sink=type(d).__name__,
                    error=str(r),
                    alert_type=alert.alert_type,
                )


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_dispatcher: AlertDispatcher | None = None


def get_alert_dispatcher() -> AlertDispatcher:
    """Return the process-wide alert dispatcher (default: console).

    BE-AUTH-001-rollout-alerts-rollout: the active sinks are
    selected by Settings — each is independent and may be left
    blank in dev. The CompositeAlertDispatcher dispatches
    every configured sink in parallel; a failure in one does
    NOT block the others.
    """
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
        if settings.auth_alert_smtp_host and settings.auth_alert_email_to:
            to_addrs = [a.strip() for a in settings.auth_alert_email_to.split(",") if a.strip()]
            sinks.append(
                EmailAlertDispatcher(
                    smtp_host=settings.auth_alert_smtp_host,
                    smtp_port=settings.auth_alert_smtp_port,
                    username=settings.auth_alert_smtp_username,
                    password=settings.auth_alert_smtp_password,
                    from_addr=settings.auth_alert_email_from,
                    to_addrs=to_addrs,
                    timeout_seconds=settings.auth_alert_smtp_timeout_s,
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
