"""Tests for the AuthAuditLogRepository (BE-AUTH-001-rollout-audit)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from niki.db.repositories.auth_audit_repo import (
    AuthAuditEvent,
    InMemoryAuthAuditLogRepository,
)


class TestAuthAuditEvent:
    def test_default_severity_is_info(self) -> None:
        ev = AuthAuditEvent(event_type="test")
        assert ev.severity == "info"

    def test_invalid_severity_raises(self) -> None:
        with pytest.raises(ValueError):
            AuthAuditEvent(event_type="x", severity="bogus")

    def test_violation_factory_applies_warning(self) -> None:
        ev = AuthAuditEvent.violation(
            event_type="parent_mismatch",
            actor_parent_id="p1",
            reason="bad",
            endpoint="POST /x",
            method="POST",
        )
        assert ev.severity == "warning"
        assert ev.event_type == "parent_mismatch"

    def test_to_dict_round_trip(self) -> None:
        ev = AuthAuditEvent.violation(
            event_type="x",
            actor_parent_id="p1",
            target_parent_id="p2",
            target_child_id="c1",
            reason="bad",
            endpoint="POST /x",
            method="POST",
            client_ip="10.0.0.1",
        )
        d = ev.to_dict()
        assert d["actor_parent_id"] == "p1"
        assert d["target_parent_id"] == "p2"
        assert d["target_child_id"] == "c1"
        assert d["client_ip"] == "10.0.0.1"
        assert d["severity"] == "warning"


class TestInMemoryAuthAuditLogRepository:
    async def test_add_assigns_id(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        ev = AuthAuditEvent.violation(
            event_type="parent_mismatch",
            actor_parent_id="p1",
            reason="bad",
            endpoint="POST /x",
            method="POST",
        )
        row_id = await repo.add(ev)
        assert isinstance(row_id, str) and len(row_id) > 0
        assert len(repo) == 1

    async def test_add_sets_created_at(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        ev = AuthAuditEvent(event_type="info_event")
        before = datetime.now(timezone.utc)
        await repo.add(ev)
        rows = await repo.list_recent()
        assert len(rows) == 1
        assert before <= rows[0]["created_at"]

    async def test_list_recent_newest_first(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        # Add in order; list_recent must return newest first.
        ids = []
        for i in range(3):
            ids.append(await repo.add(
                AuthAuditEvent(event_type=f"e{i}")
            ))
        rows = await repo.list_recent()
        assert [r["id"] for r in rows] == list(reversed(ids))

    async def test_list_recent_limit(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        for i in range(5):
            await repo.add(AuthAuditEvent(event_type="e"))
        rows = await repo.list_recent(limit=2)
        assert len(rows) == 2

    async def test_filter_by_event_type(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        await repo.add(AuthAuditEvent(event_type="a"))
        await repo.add(AuthAuditEvent(event_type="b"))
        await repo.add(AuthAuditEvent(event_type="a"))
        rows = await repo.list_recent(event_type="a")
        assert len(rows) == 2
        assert all(r["event_type"] == "a" for r in rows)

    async def test_filter_by_actor(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        await repo.add(
            AuthAuditEvent(event_type="x", actor_parent_id="p1")
        )
        await repo.add(
            AuthAuditEvent(event_type="x", actor_parent_id="p2")
        )
        await repo.add(
            AuthAuditEvent(event_type="x", actor_parent_id="p1")
        )
        rows = await repo.list_recent(actor_parent_id="p1")
        assert len(rows) == 2
        assert all(r["actor_parent_id"] == "p1" for r in rows)

    async def test_combined_filters(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        await repo.add(
            AuthAuditEvent(
                event_type="parent_mismatch",
                actor_parent_id="p1",
            )
        )
        await repo.add(
            AuthAuditEvent(
                event_type="child_not_found",
                actor_parent_id="p1",
            )
        )
        await repo.add(
            AuthAuditEvent(
                event_type="parent_mismatch",
                actor_parent_id="p2",
            )
        )
        rows = await repo.list_recent(
            event_type="parent_mismatch",
            actor_parent_id="p1",
        )
        assert len(rows) == 1
        assert rows[0]["actor_parent_id"] == "p1"
        assert rows[0]["event_type"] == "parent_mismatch"

    async def test_count_filters(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        await repo.add(AuthAuditEvent(event_type="a"))
        await repo.add(AuthAuditEvent(event_type="b"))
        await repo.add(AuthAuditEvent(event_type="a"))
        assert await repo.count() == 3
        assert await repo.count(event_type="a") == 2
        assert await repo.count(event_type="missing") == 0

    async def test_count_since(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        await repo.add(AuthAuditEvent(event_type="e"))
        # All rows are recent; since=1h ago should include them all.
        n = await repo.count(since=datetime.now(timezone.utc) - timedelta(hours=1))
        assert n == 1
        # since=in the future excludes everything.
        n_future = await repo.count(
            since=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        assert n_future == 0

    async def test_empty_repo_returns_empty_list(self) -> None:
        repo = InMemoryAuthAuditLogRepository()
        assert await repo.list_recent() == []
        assert await repo.count() == 0