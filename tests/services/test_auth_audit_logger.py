"""Tests for the AuthAuditLogger service (BE-AUTH-001-rollout-audit)."""

from __future__ import annotations

import pytest

from niki.db.repositories import (
    InMemoryAuthAuditLogRepository,
)
from niki.services.auth_audit import (
    AuthAuditLogger,
)


class TestAuthAuditLogger:
    async def test_log_violation_records_warning_severity(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        logger = AuthAuditLogger(repo=repo)
        row_id = await logger.log_violation(
            "parent_mismatch",
            actor_parent_id="p1",
            target_parent_id="p2",
            reason="bad",
            endpoint="POST /api/v1/children/register/p2",
            method="POST",
            client_ip="10.0.0.1",
        )
        rows = await repo.list_recent()
        assert len(rows) == 1
        assert rows[0]["id"] == row_id
        assert rows[0]["severity"] == "warning"
        assert rows[0]["event_type"] == "parent_mismatch"
        assert rows[0]["target_parent_id"] == "p2"
        assert rows[0]["client_ip"] == "10.0.0.1"

    async def test_log_event_default_severity_info(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        logger = AuthAuditLogger(repo=repo)
        await logger.log_event(
            "login_success",
            actor_parent_id="p1",
            reason="ok",
            endpoint="POST /auth/login",
            method="POST",
        )
        rows = await repo.list_recent()
        assert rows[0]["severity"] == "info"

    async def test_log_event_severity_override(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        logger = AuthAuditLogger(repo=repo)
        await logger.log_event(
            "brute_force_detected",
            actor_parent_id="p1",
            severity="critical",
            reason="...",
            endpoint="POST /auth/login",
            method="POST",
        )
        rows = await repo.list_recent()
        assert rows[0]["severity"] == "critical"

    async def test_persist_failure_is_swallowed(self) -> None:
        """A failing repo must not raise — the caller has already
        decided to return 4xx and audit failure must not change that."""

        class BoomRepo:
            async def add(self, event):
                raise RuntimeError("DB is down")

        logger = AuthAuditLogger(repo=BoomRepo())
        # Should NOT raise.
        result = await logger.log_violation(
            "x",
            actor_parent_id="p1",
            reason="r",
            endpoint="POST /x",
            method="POST",
        )
        assert result is None

    async def test_lazy_repo_resolution(self) -> None:
        """If constructed with repo=None, the logger resolves it
        from the process-wide singleton on first use."""
        logger = AuthAuditLogger()  # no repo injected
        # First call should resolve via get_auth_audit_log_repository()
        await logger.log_event(
            "e", actor_parent_id="p1", reason="r",
            endpoint="GET /x", method="GET",
        )
        assert logger.repo is not None
        rows = await logger.repo.list_recent()
        assert len(rows) == 1