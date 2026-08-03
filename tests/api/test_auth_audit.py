"""Tests for the auth audit query endpoint and the lateral-check
audit hookups (BE-AUTH-001-rollout-audit)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.repositories import (
    AuthAuditEvent,
    get_auth_audit_log_repository,
)
from niki.main import create_app
from niki.services.child_registry import (
    InMemoryChildRegistry,
    set_child_registry,
)


# ---------------------------------------------------------------------------
# Shared fixtures — a real registry + an auth override that returns
# a deterministic parent id so we can attribute audit events.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a TestClient bound to parent id `p_actor`.

    We unset NIKI_TEST_FAKE_AUTH so the real lateral-auth check
    runs (otherwise get_owned_child_id short-circuits). We still
    override get_current_parent directly to inject a deterministic
    caller id without going through real JWT signing.
    """
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    set_child_registry(InMemoryChildRegistry())
    app = create_app()

    def fake():
        return ParentAccount(
            id="p_actor", name="Test",
            id_number_masked="****", phone_masked="****",
        )

    app.dependency_overrides[get_current_parent] = fake
    return TestClient(app)


# ---------------------------------------------------------------------------
# Lateral-check hook tests
# ---------------------------------------------------------------------------


class TestLateralCheckHooks:
    """Verify that lateral-auth rejections are captured as audit events."""

    def test_parent_child_mismatch_returns_403(
        self, client: TestClient
    ) -> None:
        """Try to access a child owned by another parent."""
        registry = InMemoryChildRegistry()
        registry.register("c_victim", "p_victim")
        set_child_registry(registry)

        r = client.get(
            "/api/v1/dashboard/weekly-report/c_victim",
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "PARENT_CHILD_MISMATCH"

    async def test_parent_mismatch_records_audit(
        self, client: TestClient
    ) -> None:
        """register_child with mismatched parent_id must log audit."""
        r = client.post(
            "/api/v1/children/register/p_someone_else",
            json={"child_id": "c1"},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "PARENT_MISMATCH"

        # Audit log should now contain a parent_mismatch event
        # attributed to p_actor → p_someone_else.
        repo = get_auth_audit_log_repository()
        rows = await repo.list_recent(event_type="parent_mismatch")
        assert len(rows) >= 1
        ev = rows[0]
        assert ev["actor_parent_id"] == "p_actor"
        assert ev["target_parent_id"] == "p_someone_else"
        assert ev["severity"] == "warning"
        assert ev["method"] == "POST"
        assert "register" in ev["endpoint"]

    async def test_parent_mismatch_records_audit_on_list(
        self, client: TestClient
    ) -> None:
        """list_children with mismatched parent_id must also log."""
        r = client.get(
            "/api/v1/children/list/p_someone_else",
        )
        assert r.status_code == 403

        repo = get_auth_audit_log_repository()
        rows = await repo.list_recent(event_type="parent_mismatch")
        # Should have at least one — from the GET above.
        assert len(rows) >= 1
        # GET is the most recent.
        assert rows[0]["method"] == "GET"


# ---------------------------------------------------------------------------
# Query endpoint tests
# ---------------------------------------------------------------------------


async def _seed_repo(rows: list[AuthAuditEvent]) -> None:
    repo = get_auth_audit_log_repository()
    for row in rows:
        await repo.add(row)


class TestAuthAuditRecentEndpoint:
    def test_empty_log_returns_empty_list(self, client: TestClient) -> None:
        r = client.get("/api/v1/auth/audit/recent")
        assert r.status_code == 200
        body = r.json()
        assert body["events"] == []
        assert body["count"] == 0
        assert body["actor_parent_id"] == "p_actor"

    async def test_default_scoping_is_self_only(
        self, client: TestClient
    ) -> None:
        """Caller should NOT see other parents' events by default."""
        await _seed_repo([
            AuthAuditEvent(
                event_type="parent_mismatch",
                actor_parent_id="p_actor",
            ),
            AuthAuditEvent(
                event_type="child_not_found",
                actor_parent_id="p_actor",
            ),
            AuthAuditEvent(
                event_type="parent_mismatch",
                actor_parent_id="p_someone_else",
            ),
        ])

        r = client.get("/api/v1/auth/audit/recent")
        assert r.status_code == 200
        body = r.json()
        # Must NOT include p_someone_else's event.
        for ev in body["events"]:
            assert ev["actor_parent_id"] == "p_actor"
        assert body["count"] == 2

    async def test_event_type_filter(self, client: TestClient) -> None:
        await _seed_repo([
            AuthAuditEvent(
                event_type="parent_mismatch",
                actor_parent_id="p_actor",
            ),
            AuthAuditEvent(
                event_type="child_not_found",
                actor_parent_id="p_actor",
            ),
        ])

        r = client.get(
            "/api/v1/auth/audit/recent",
            params={"event_type": "parent_mismatch"},
        )
        body = r.json()
        assert body["count"] == 1
        assert body["events"][0]["event_type"] == "parent_mismatch"
        assert body["event_type_filter"] == "parent_mismatch"

    async def test_limit_is_respected(self, client: TestClient) -> None:
        rows = [
            AuthAuditEvent(
                event_type=f"e{i}",
                actor_parent_id="p_actor",
            )
            for i in range(10)
        ]
        await _seed_repo(rows)

        r = client.get(
            "/api/v1/auth/audit/recent", params={"limit": 3}
        )
        assert r.json()["count"] == 3

    def test_limit_cap_200(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/auth/audit/recent", params={"limit": 500}
        )
        # Query(le=200) rejects before our explicit check.
        assert r.status_code == 400

    def test_limit_one_minimum(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/auth/audit/recent", params={"limit": 0}
        )
        assert r.status_code == 400

    async def test_response_serialises_datetime(self, client: TestClient) -> None:
        await _seed_repo([
            AuthAuditEvent(
                event_type="parent_mismatch",
                actor_parent_id="p_actor",
            ),
        ])
        r = client.get("/api/v1/auth/audit/recent")
        body = r.json()
        assert body["count"] == 1
        # created_at must be ISO 8601 string for JSON compat.
        assert isinstance(body["events"][0]["created_at"], str)
        assert "T" in body["events"][0]["created_at"]