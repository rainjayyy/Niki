"""AuditLogService — the single entry point for audit events (BE-COMP-004).

Every compliance-relevant domain event goes through `log_event()`:
consent signatures, deletion requests, safety blocks, high-risk
alerts, logins, and (dual-written from `AuthAuditLogger`) lateral-auth
violations. The result is one table a parent or support engineer can
query for a whole-account history.

Two invariants this service owns
-------------------------------
1. **Never raise.** An audit write must not change the response the
   user already earned. Failures are logged and swallowed, exactly
   like `AuthAuditLogger._safe_add`.
2. **Never persist PII.** `log_event` scrubs the metadata dict of
   known-sensitive keys before it reaches the DB, so a careless call
   site can't leak a child's utterance or a parent's phone number
   into the widely-read audit table.

Call sites pass an `event_type` string; the constants live in
`niki.db.models.audit_log` so two domains can't spell the same event
differently.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

from niki.db.models.audit_log import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
)
from niki.db.repositories.audit_log_repo import (
    AuditEvent,
    AuditLogRepository,
    get_audit_log_repository,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "AuditLogService",
    "get_audit_log_service",
    "set_audit_log_service",
]


# Metadata keys that must never reach `audit_logs`. The unified table
# is read by support tooling and (via /admin/audit) by parents, so it
# is deliberately held to a stricter standard than the domain tables:
# no free child text, no raw phone, no signature payload.
_PII_METADATA_KEYS = frozenset({
    "original_text",
    "text",
    "message",
    "content",
    "utterance",
    "child_text",
    "transcript",
    "phone",
    "phone_number",
    "id_number",
    "typed_signature",
    "signature",
    "email",
    "address",
    "name",
})

_REDACTED = "[redacted]"

# Field names the structlog line already occupies. A metadata key that
# collides with one of them would make `log_fn(**metadata)` raise
# TypeError ("got multiple values for keyword argument") — which, since
# the log line is emitted *before* the DB write, would lose the audit
# row entirely. So colliding keys are prefixed for the log line only;
# the persisted metadata keeps the caller's spelling.
_RESERVED_LOG_FIELDS = frozenset({
    "severity",
    "actor_parent_id",
    "target_parent_id",
    "target_child_id",
    "endpoint",
    "method",
    "reason",
    "client_ip",
    "event",
    "level",
    "timestamp",
    "logger",
})

# Metadata values longer than this are truncated — a long string is
# almost always free text that slipped past the key check.
_MAX_METADATA_STR = 200


class AuditLogService:
    """Persist unified audit events + emit a matching structlog line."""

    def __init__(self, repo: AuditLogRepository | None = None) -> None:
        self._repo = repo

    @property
    def repo(self) -> AuditLogRepository:
        if self._repo is None:
            self._repo = get_audit_log_repository()
        return self._repo

    async def log_event(
        self,
        event_type: str,
        *,
        severity: str = SEVERITY_INFO,
        actor_parent_id: str | None = None,
        target_parent_id: str | None = None,
        target_child_id: str | None = None,
        endpoint: str | None = None,
        method: str | None = None,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
        client_ip: str | None = None,
    ) -> Optional[str]:
        """Record one audit event.

        Returns the new row id, or `None` if the write failed (or the
        event was malformed). Callers do NOT need to handle the None
        case — an audit miss is logged here and must never change the
        caller's own response.
        """
        try:
            event = AuditEvent(
                event_type=event_type,
                severity=severity,
                actor_parent_id=actor_parent_id,
                target_parent_id=target_parent_id,
                target_child_id=target_child_id,
                endpoint=endpoint,
                method=method,
                reason=reason,
                metadata=_scrub_metadata(metadata),
                client_ip=client_ip,
            )
        except ValueError as exc:
            # A bad event_type / severity is a programming error, but
            # not one worth 500-ing a parent's request over.
            logger.error(
                "audit.invalid_event",
                error=str(exc),
                event_type=event_type,
                severity=severity,
            )
            return None

        # 1. Structured log first, so the record reaches the log
        # shipper even when the DB write below fails.
        log_fn = (
            logger.warning
            if event.severity == SEVERITY_WARNING
            else logger.critical
            if event.severity == SEVERITY_CRITICAL
            else logger.info
        )
        log_fn(
            f"audit.{event.event_type}",
            severity=event.severity,
            actor_parent_id=event.actor_parent_id,
            target_parent_id=event.target_parent_id,
            target_child_id=event.target_child_id,
            endpoint=event.endpoint,
            method=event.method,
            reason=event.reason,
            client_ip=event.client_ip,
            **_rename_reserved(event.metadata),
        )

        # 2. Persist — swallow failures.
        try:
            return await self.repo.add(event)
        except Exception as exc:  # pragma: no cover — DB failure path
            logger.error(
                "audit.persist_failed",
                error=str(exc),
                event_type=event.event_type,
            )
            return None

    async def list_events(
        self,
        *,
        limit: int = 50,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
        since: Any = None,
    ) -> list[dict[str, Any]]:
        """Read events back, newest-first. Thin pass-through to the repo."""
        return await self.repo.list_events(
            limit=limit,
            event_type=event_type,
            actor_parent_id=actor_parent_id,
            since=since,
        )


def _scrub_metadata(
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Drop / redact PII-shaped entries from an event's metadata.

    Two rules, applied in order:
      1. A key on the `_PII_METADATA_KEYS` denylist is replaced with
         `"[redacted]"` (kept, not dropped, so the audit shows that
         *something* was there — useful when debugging a call site).
      2. Any remaining string longer than `_MAX_METADATA_STR` is
         truncated, because long strings are free text by nature.
    """
    if not metadata:
        return {}
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.lower() in _PII_METADATA_KEYS:
            out[key] = _REDACTED
            continue
        if isinstance(value, str) and len(value) > _MAX_METADATA_STR:
            out[key] = value[:_MAX_METADATA_STR] + "…"
            continue
        out[key] = value
    return out


def _rename_reserved(metadata: dict[str, Any]) -> dict[str, Any]:
    """Prefix metadata keys that would clash with the log line's own.

    `meta_endpoint=fcm.googleapis.com` next to `endpoint=/api/v1/...`
    reads fine in a log viewer, and — unlike the TypeError this
    avoids — it doesn't cost us the audit row.
    """
    return {
        (f"meta_{key}" if key in _RESERVED_LOG_FIELDS else key): value
        for key, value in metadata.items()
    }


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_service: AuditLogService | None = None


def get_audit_log_service() -> AuditLogService:
    """Return the process-wide AuditLogService."""
    global _service
    if _service is None:
        _service = AuditLogService()
    return _service


def set_audit_log_service(service: AuditLogService) -> None:
    """Override the process-wide AuditLogService (tests)."""
    global _service
    _service = service
