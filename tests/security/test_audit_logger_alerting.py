"""Tests for the AuthAuditLogger alert hook
(BE-AUTH-001-rollout-alerts)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from niki.db.repositories import (
    InMemoryAuthAuditLogRepository,
)
from niki.security.alerts import SecurityAlert
from niki.security.rate_tracker import ViolationRateTracker
from niki.services.auth_audit import AuthAuditLogger


def _capturing_dispatcher() -> tuple[AsyncMock, object]:
    """Return (mock, dispatcher) — mock records every dispatched alert."""
    captured: list[SecurityAlert] = []
    mock = AsyncMock(side_effect=lambda a: captured.append(a))
    return mock, mock


class TestAuditLoggerAlerting:
    async def test_below_threshold_no_alert(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        tracker = ViolationRateTracker(window_seconds=300, burst_count=3)
        dispatcher_mock, _ = _capturing_dispatcher()
        logger = AuthAuditLogger(
            repo=repo,
            rate_tracker=tracker,
            dispatcher=dispatcher_mock,
        )

        # Two violations — below burst_count=3.
        for _ in range(2):
            await logger.log_violation(
                "parent_child_mismatch",
                actor_parent_id="p1",
                reason="x",
                endpoint="POST /x",
                method="POST",
            )
        dispatcher_mock.dispatch.assert_not_awaited()

    async def test_at_threshold_fires_alert(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        tracker = ViolationRateTracker(window_seconds=300, burst_count=3)
        dispatcher_mock, _ = _capturing_dispatcher()
        logger = AuthAuditLogger(
            repo=repo,
            rate_tracker=tracker,
            dispatcher=dispatcher_mock,
        )

        for i in range(3):
            await logger.log_violation(
                "parent_child_mismatch",
                actor_parent_id="p1",
                reason=f"violation {i}",
                endpoint="POST /x",
                method="POST",
                target_child_id="c1",
            )

        # Note: AsyncMock's child attribute `.dispatch` is itself an
        # AsyncMock — that's where the await registers.
        dispatcher_mock.dispatch.assert_awaited_once()
        alert: SecurityAlert = dispatcher_mock.dispatch.await_args.args[0]
        assert alert.alert_type == "lateral_auth_burst"
        assert alert.severity == "critical"
        assert alert.actor_parent_id == "p1"
        assert alert.target_child_id == "c1"
        assert alert.event_count == 3
        assert alert.window_seconds == 300

    async def test_separate_actors_independent_threshold(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        tracker = ViolationRateTracker(window_seconds=300, burst_count=3)
        dispatcher_mock, _ = _capturing_dispatcher()
        logger = AuthAuditLogger(
            repo=repo,
            rate_tracker=tracker,
            dispatcher=dispatcher_mock,
        )

        # p1 fires 3 violations, p2 fires 1.
        for _ in range(3):
            await logger.log_violation(
                "x",
                actor_parent_id="p1",
                reason="r",
                endpoint="POST /x",
                method="POST",
            )
        await logger.log_violation(
            "x",
            actor_parent_id="p2",
            reason="r",
            endpoint="POST /x",
            method="POST",
        )

        # Exactly one alert fired, attributed to p1.
        dispatcher_mock.dispatch.assert_awaited_once()
        alert = dispatcher_mock.dispatch.await_args.args[0]
        assert alert.actor_parent_id == "p1"

    async def test_info_event_does_not_trigger_alert(self) -> None:
        """Non-violation events must NOT feed the rate tracker."""
        repo = InMemoryAuthAuditLogRepository()
        tracker = ViolationRateTracker(window_seconds=300, burst_count=3)
        dispatcher_mock, _ = _capturing_dispatcher()
        logger = AuthAuditLogger(
            repo=repo,
            rate_tracker=tracker,
            dispatcher=dispatcher_mock,
        )

        for _ in range(10):
            await logger.log_event(
                "login_success",
                actor_parent_id="p1",
                reason="ok",
                endpoint="POST /auth/login",
                method="POST",
            )
        dispatcher_mock.dispatch.assert_not_awaited()

    async def test_dispatcher_failure_does_not_break_logging(
        self,
    ) -> None:
        """A broken dispatcher must not prevent subsequent
        audit logs from being recorded."""
        repo = InMemoryAuthAuditLogRepository()
        tracker = ViolationRateTracker(window_seconds=300, burst_count=3)

        broken_dispatcher = AsyncMock(side_effect=RuntimeError("webhook down"))
        logger = AuthAuditLogger(
            repo=repo,
            rate_tracker=tracker,
            dispatcher=broken_dispatcher,
        )

        for _ in range(3):
            await logger.log_violation(
                "x",
                actor_parent_id="p1",
                reason="r",
                endpoint="POST /x",
                method="POST",
            )
        # Repo still got the 3 events.
        rows = await repo.list_recent()
        assert len(rows) == 3

    async def test_alert_payload_contains_event_context(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        tracker = ViolationRateTracker(window_seconds=300, burst_count=2)
        dispatcher_mock, _ = _capturing_dispatcher()
        logger = AuthAuditLogger(
            repo=repo,
            rate_tracker=tracker,
            dispatcher=dispatcher_mock,
        )

        await logger.log_violation(
            "parent_child_mismatch",
            actor_parent_id="p_actor",
            reason="tried to access child of p_victim",
            endpoint="GET /dashboard/weekly-report/c_victim",
            method="GET",
            target_parent_id="p_victim",
            target_child_id="c_victim",
            client_ip="10.0.0.5",
        )
        await logger.log_violation(
            "parent_child_mismatch",
            actor_parent_id="p_actor",
            reason="second attempt",
            endpoint="GET /treehole/summaries/c_victim",
            method="GET",
            target_parent_id="p_victim",
            target_child_id="c_victim",
            client_ip="10.0.0.5",
        )

        dispatcher_mock.dispatch.assert_awaited_once()
        alert: SecurityAlert = dispatcher_mock.dispatch.await_args.args[0]
        assert alert.actor_parent_id == "p_actor"
        assert alert.target_parent_id == "p_victim"
        assert alert.target_child_id == "c_victim"
        assert alert.client_ip == "10.0.0.5"
        assert alert.event_count == 2
        assert "p_actor" in alert.title
        assert "p_victim" in alert.detail
