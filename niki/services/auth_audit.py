"""AuthAuditLogger service (BE-AUTH-001-rollout-audit).

Wraps the AuthAuditLogRepository with a high-level API that
    1. Persists the event to the audit log
    2. Emits a structured structlog record (for ELK / Loki)
    3. BE-AUTH-001-rollout-alerts: feeds the ViolationRateTracker
       and dispatches a SecurityAlert when a burst threshold is
       crossed for the same actor.
    4. BE-COMP-004: mirrors the event into the unified `audit_logs`
       table so consent / deletion / safety / auth all share one
       queryable history. `auth_audit_logs` remains the fast path
       for the alerting logic in this module.

Call sites use `log_violation()` for the common lateral-auth case
or `log_event()` for generic info-level events. Audit-write
failures are caught and logged but NEVER raised — a failed write
must not change the 4xx response the user already saw.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog

from niki.db.repositories.auth_audit_repo import (
    AuthAuditEvent,
    AuthAuditLogRepository,
    SEVERITY_WARNING,
    get_auth_audit_log_repository,
)
from niki.feature_flags import get_feature_flag_service
from niki.metrics import get_registry
from niki.security.alerts import (
    AlertDispatcher,
    SecurityAlert,
    get_alert_dispatcher,
)
from niki.security.rate_tracker import ViolationRateTracker

logger = structlog.get_logger(__name__)


class AuthAuditLogger:
    """Persist auth audit events + emit structured log lines + alert."""

    def __init__(
        self,
        repo: AuthAuditLogRepository | None = None,
        *,
        rate_tracker: ViolationRateTracker | None = None,
        dispatcher: AlertDispatcher | None = None,
    ) -> None:
        self._repo = repo
        self._dispatcher = dispatcher
        self._rate_tracker = rate_tracker

    @property
    def repo(self) -> AuthAuditLogRepository:
        if self._repo is None:
            self._repo = get_auth_audit_log_repository()
        return self._repo

    @property
    def dispatcher(self) -> AlertDispatcher:
        if self._dispatcher is None:
            self._dispatcher = get_alert_dispatcher()
        return self._dispatcher

    @property
    def rate_tracker(self) -> ViolationRateTracker:
        if self._rate_tracker is None:
            from niki.config.settings import get_settings

            s = get_settings()
            self._rate_tracker = ViolationRateTracker(
                window_seconds=s.auth_alert_window_seconds,
                burst_count=s.auth_alert_burst_count,
            )
        return self._rate_tracker

    async def log_violation(
        self,
        event_type: str,
        *,
        actor_parent_id: str | None,
        reason: str,
        endpoint: str,
        method: str,
        target_parent_id: str | None = None,
        target_child_id: str | None = None,
        client_ip: str | None = None,
    ) -> Optional[str]:
        """Record a `warning`-severity auth violation.

        Returns the row id on success, None on failure (the caller
        is expected to raise 4xx either way — never let an audit
        failure block the auth response).
        """
        event = AuthAuditEvent.violation(
            event_type=event_type,
            actor_parent_id=actor_parent_id,
            reason=reason,
            endpoint=endpoint,
            method=method,
            target_parent_id=target_parent_id,
            target_child_id=target_child_id,
            client_ip=client_ip,
        )
        return await self._safe_add(event)

    async def log_event(
        self,
        event_type: str,
        *,
        actor_parent_id: str | None = None,
        reason: str = "",
        endpoint: str = "",
        method: str = "",
        severity: str = "info",
        target_parent_id: str | None = None,
        target_child_id: str | None = None,
        client_ip: str | None = None,
    ) -> Optional[str]:
        """Record a generic (non-violation) auth event."""
        event = AuthAuditEvent(
            event_type=event_type,
            severity=severity,
            actor_parent_id=actor_parent_id,
            target_parent_id=target_parent_id,
            target_child_id=target_child_id,
            endpoint=endpoint,
            method=method,
            reason=reason,
            client_ip=client_ip,
        )
        return await self._safe_add(event)

    async def _safe_add(self, event: AuthAuditEvent) -> Optional[str]:
        """Persist + log + maybe alert; never raise."""
        # 1. Structured log first so even if the DB write fails
        # the record reaches stdout / log shipper.
        log_fn = (
            logger.warning
            if event.severity == "warning"
            else logger.critical
            if event.severity == "critical"
            else logger.info
        )
        log_fn(
            f"auth.audit.{event.event_type}",
            severity=event.severity,
            actor_parent_id=event.actor_parent_id,
            target_parent_id=event.target_parent_id,
            target_child_id=event.target_child_id,
            endpoint=event.endpoint,
            method=event.method,
            reason=event.reason,
            client_ip=event.client_ip,
        )

        # 2. Persist — swallow exceptions so the caller can still
        # return its 4xx / continue.
        try:
            row_id = await self.repo.add(event)
        except Exception as exc:  # pragma: no cover — DB failure path
            logger.error(
                "auth.audit.persist_failed",
                error=str(exc),
                event_type=event.event_type,
            )
            row_id = None

        # 2b. BE-COMP-004: dual-write to the unified `audit_logs`
        # table so the parent-facing cross-domain history includes
        # auth events alongside consent / deletion / safety. The
        # narrow `auth_audit_logs` write above stays authoritative
        # for the alerting path below — it is a single-table index
        # scan and must not pay for the wider table.
        await self._mirror_to_unified_audit(event)

        # 2c. BE-API-009: count every audit event into the
        # `niki_auth_audit_total{event_type, severity}` counter.
        try:
            get_registry().AUTH_AUDIT_TOTAL.labels(
                event_type=event.event_type,
                severity=event.severity,
            ).inc()
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "auth.audit.metrics_increment_failed",
                error=str(exc),
                event_type=event.event_type,
            )

        # 3. NEW (BE-AUTH-001-rollout-alerts): feed the rate tracker
        # for warning-severity events and dispatch an alert when
        # the burst threshold is crossed.
        #
        # BE-MISC-002: the `silence_alerts` feature flag can
        # short-circuit the dispatcher step (but never the audit
        # write above). The rate tracker still updates so the
        # moment the flag is flipped back on we resume from
        # accurate counts.
        if event.severity == SEVERITY_WARNING and event.actor_parent_id:
            await self._maybe_alert(event)

        return row_id

    async def _mirror_to_unified_audit(self, event: AuthAuditEvent) -> None:
        """Copy an auth event into the BE-COMP-004 `audit_logs` table.

        Best-effort and silent: the auth trail is already durable at
        this point, so a failure here only costs the cross-domain
        view a row. Imported lazily to keep the module import graph
        acyclic (`AuditLogService` does not depend on auth, but the
        two singletons are wired up in the same conftest fixture).
        """
        try:
            from niki.services.audit_log import get_audit_log_service

            await get_audit_log_service().log_event(
                event.event_type,
                severity=event.severity,
                actor_parent_id=event.actor_parent_id,
                target_parent_id=event.target_parent_id,
                target_child_id=event.target_child_id,
                endpoint=event.endpoint or None,
                method=event.method or None,
                reason=event.reason,
                client_ip=event.client_ip,
                metadata={"source": "auth_audit_logs"},
            )
        except Exception as exc:  # pragma: no cover — defence in depth
            logger.warning(
                "auth.audit.unified_mirror_failed",
                error=str(exc),
                event_type=event.event_type,
            )

    async def _maybe_alert(self, event: AuthAuditEvent) -> None:
        """Record violation in rate tracker; dispatch alert if burst."""
        try:
            count = self.rate_tracker.record(event.actor_parent_id or "")
            if count < self.rate_tracker.burst_count:
                return
            # Re-check should_alert in case two violations land on
            # the same threshold tick — only fire once per crossing.
            if not self.rate_tracker.should_alert(event.actor_parent_id or ""):
                return
            # BE-MISC-002: ops can flip `silence_alerts` to suppress
            # outbound alert dispatch during incident response or
            # staging-load tests. The rate tracker above is
            # intentionally NOT skipped — when the flag flips back
            # off we resume from accurate counts.
            if get_feature_flag_service().is_enabled(
                event.actor_parent_id,
                "silence_alerts",
            ):
                logger.info(
                    "auth.audit.alert_silenced_by_flag",
                    actor_parent_id=event.actor_parent_id,
                    event_type=event.event_type,
                )
                return
            alert = SecurityAlert(
                alert_type="lateral_auth_burst",
                severity="critical",
                actor_parent_id=event.actor_parent_id,
                target_parent_id=event.target_parent_id,
                target_child_id=event.target_child_id,
                client_ip=event.client_ip,
                event_count=count,
                window_seconds=self.rate_tracker.window_seconds,
                detected_at=datetime.now(timezone.utc),
                title=(f"Lateral-auth burst: {event.actor_parent_id} x{count}"),
                detail=(
                    f"Actor {event.actor_parent_id!r} accumulated "
                    f"{count} lateral-auth violations within "
                    f"{self.rate_tracker.window_seconds}s. "
                    f"Latest: {event.event_type} via {event.method} "
                    f"{event.endpoint} (target_parent="
                    f"{event.target_parent_id!r}, target_child="
                    f"{event.target_child_id!r}, client_ip="
                    f"{event.client_ip!r})."
                ),
            )
            await self.dispatcher.dispatch(alert)
            # BE-RT-002: also publish to the in-process SSE
            # broker so subscribed parents see the alert in
            # < 5s. Failures are non-fatal — the broker is a
            # best-effort side-channel.
            try:
                from niki.realtime.alert_broker import (
                    get_alert_broker,
                )

                get_alert_broker().publish(alert)
            except Exception as broker_exc:  # pragma: no cover
                logger.warning(
                    "auth.audit.broker_publish_failed",
                    error=str(broker_exc),
                )
        except Exception as exc:  # pragma: no cover
            logger.error(
                "auth.audit.alert_dispatch_failed",
                error=str(exc),
                event_type=event.event_type,
            )


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_logger: AuthAuditLogger | None = None


def get_auth_audit_logger() -> AuthAuditLogger:
    """Return the process-wide AuthAuditLogger."""
    global _logger
    if _logger is None:
        _logger = AuthAuditLogger()
    return _logger


def set_auth_audit_logger(audit_logger: AuthAuditLogger) -> None:
    """Override the process-wide AuthAuditLogger (tests)."""
    global _logger
    _logger = audit_logger
