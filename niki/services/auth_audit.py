"""AuthAuditLogger service (BE-AUTH-001-rollout-audit).

Wraps the AuthAuditLogRepository with a high-level API that
    1. Persists the event to the audit log
    2. Emits a structured structlog record (for ELK / Loki)

Call sites use `log_violation()` for the common lateral-auth case
or `log_event()` for generic info-level events. Audit-write
failures are caught and logged but NEVER raised — a failed write
must not change the 4xx response the user already saw.
"""

from __future__ import annotations

from typing import Optional

import structlog

from niki.db.repositories.auth_audit_repo import (
    AuthAuditEvent,
    AuthAuditLogRepository,
    get_auth_audit_log_repository,
)

logger = structlog.get_logger(__name__)


class AuthAuditLogger:
    """Persist auth audit events + emit structured log lines."""

    def __init__(self, repo: AuthAuditLogRepository | None = None) -> None:
        self._repo = repo

    @property
    def repo(self) -> AuthAuditLogRepository:
        if self._repo is None:
            self._repo = get_auth_audit_log_repository()
        return self._repo

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
        """Persist + log; never raise."""
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
            return await self.repo.add(event)
        except Exception as exc:  # pragma: no cover — DB failure path
            logger.error(
                "auth.audit.persist_failed",
                error=str(exc),
                event_type=event.event_type,
            )
            return None


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