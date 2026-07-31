"""Tests for the BE-PUSH-005 notification-center endpoints.

Three endpoints under test:

  GET  /api/v1/notifications            — cursor-paginated list
  POST /api/v1/notifications/{id}/read  — mark single notification read
  POST /api/v1/notifications/read-all   — mark every unread notification read

Plus the lateral-auth story: 404 (not 403) when a parent tries to
read another parent's notification. And the cursor story: stale /
tampered cursors return 400 INVALID_CURSOR, not 500.

Each test seeds its own `InMemoryDashboardRepository` so cross-test
state doesn't leak (the repo is a process-wide singleton otherwise).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from niki.api._cursor import decode_cursor, encode_cursor
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.repositories import (
    InMemoryDashboardRepository,
    set_dashboard_repository,
)
from niki.main import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_alert(
    repo: InMemoryDashboardRepository,
    parent_id: str,
    *,
    severity: str = "info",
    alert_type: str = "safety_valve",
    payload_extra: dict | None = None,
    created_offset: timedelta = timedelta(seconds=0),
) -> str:
    """Insert an alert row directly via the in-memory repo."""
    payload = {"severity": severity}
    if payload_extra:
        payload.update(payload_extra)
    row_id = f"alert-{len(repo._alerts) + 1}-{parent_id}"
    repo._alerts[row_id] = {
        "id": row_id,
        "alert_type": alert_type,
        "child_id": f"child-of-{parent_id}",
        "parent_id": parent_id,
        "payload": dict(payload),
        "created_at": datetime.now(timezone.utc) + created_offset,
        "delivered_at": None,
        "read_at": None,
    }
    return row_id


@pytest.fixture
def repo() -> InMemoryDashboardRepository:
    storage = InMemoryDashboardRepository()
    set_dashboard_repository(storage)
    return storage


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p-1", name="T",
        id_number_masked="****", phone_masked="****",
    )
    return TestClient(app)


@pytest.fixture
def client_other_parent() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p-other", name="Other",
        id_number_masked="****", phone_masked="****",
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Cursor helper (unit)
# ---------------------------------------------------------------------------


class TestCursorHelper:
    def test_roundtrip(self) -> None:
        ts = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
        encoded = encode_cursor("alert-abc", ts)
        position = decode_cursor(encoded)
        assert position.id == "alert-abc"
        assert position.ts == ts

    def test_tampered_signature_raises(self) -> None:
        ts = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
        encoded = encode_cursor("alert-abc", ts)
        # Flip one char of the signature.
        head, _, sig = encoded.rpartition(".")
        tampered = f"{head}.{('A' if sig[0] != 'A' else 'B')}{sig[1:]}"
        with pytest.raises(Exception):
            decode_cursor(tampered)

    def test_missing_dot_raises(self) -> None:
        with pytest.raises(Exception):
            decode_cursor("nodot")

    def test_garbage_payload_raises(self) -> None:
        with pytest.raises(Exception):
            decode_cursor("@@@.@@@")


# ---------------------------------------------------------------------------
# List endpoint
# ---------------------------------------------------------------------------


class TestListNotifications:
    def test_empty_parent_returns_empty_list(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        r = client.get("/api/v1/notifications")
        assert r.status_code == 200
        body = r.json()
        assert body["notifications"] == []
        assert body["has_more"] is False
        assert body["next_cursor"] is None

    def test_default_limit_is_20(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        for i in range(25):
            _make_alert(repo, "p-1", created_offset=timedelta(seconds=i))
        r = client.get("/api/v1/notifications")
        body = r.json()
        assert len(body["notifications"]) == 20
        assert body["has_more"] is True
        assert body["next_cursor"] is not None

    def test_limit_too_large_returns_422(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        r = client.get("/api/v1/notifications?limit=500")
        assert r.status_code == 422

    def test_limit_below_one_returns_422(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        r = client.get("/api/v1/notifications?limit=0")
        assert r.status_code == 422

    def test_other_parents_alerts_excluded(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        _make_alert(repo, "p-1", severity="critical")
        _make_alert(repo, "p-1", severity="info")
        _make_alert(repo, "p-other", severity="critical")
        r = client.get("/api/v1/notifications")
        body = r.json()
        ids = [n["id"] for n in body["notifications"]]
        assert len(ids) == 2
        # All returned rows belong to p-1.
        for row in repo._alerts.values():
            if row["id"] in ids:
                assert row["parent_id"] == "p-1"


class TestCursorPagination:
    def test_forward_pagination_walks_all_pages(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        # 10 alerts with monotonically decreasing timestamps so the
        # DESC order is deterministic.
        for i in range(10):
            _make_alert(
                repo, "p-1",
                severity="info",
                created_offset=timedelta(seconds=i * 10),
            )

        seen_ids: list[str] = []
        cursor = None
        for _ in range(3):  # 5 + 5 + (final empty)
            url = "/api/v1/notifications?limit=5"
            if cursor:
                url += f"&cursor={cursor}"
            r = client.get(url)
            assert r.status_code == 200
            body = r.json()
            ids = [n["id"] for n in body["notifications"]]
            seen_ids.extend(ids)
            cursor = body["next_cursor"]
            if not body["has_more"]:
                break

        # All 10 alerts walked, no duplicates, no extras.
        assert len(seen_ids) == 10
        assert len(set(seen_ids)) == 10

    def test_final_page_has_no_more(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        for i in range(5):
            _make_alert(repo, "p-1", created_offset=timedelta(seconds=i))
        r = client.get("/api/v1/notifications?limit=10")
        body = r.json()
        assert body["has_more"] is False
        assert body["next_cursor"] is None
        assert len(body["notifications"]) == 5

    def test_stale_cursor_returns_empty_list(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        # Build a cursor pointing to a timestamp from the year 2000 —
        # no row in the fixture is older, so the next page must be
        # empty (not an error).
        ts_old = datetime(2000, 1, 1, tzinfo=timezone.utc)
        stale = encode_cursor("never-existed", ts_old)
        r = client.get(f"/api/v1/notifications?cursor={stale}")
        assert r.status_code == 200
        body = r.json()
        assert body["notifications"] == []
        assert body["has_more"] is False

    def test_tampered_cursor_returns_400(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        bad = "this-is-not-a-cursor"
        r = client.get(f"/api/v1/notifications?cursor={bad}")
        assert r.status_code == 400
        body = r.json()["detail"]
        assert body["error"] == "INVALID_CURSOR"
        # i18n message is the Chinese default (no Accept-Language).
        assert body["message"]

    def test_signature_tampered_returns_400(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        # Build a valid cursor then flip a signature byte.
        ts = datetime.now(timezone.utc)
        valid = encode_cursor("alert-x", ts)
        head, _, sig = valid.rpartition(".")
        bad_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
        tampered = f"{head}.{bad_sig}"
        r = client.get(f"/api/v1/notifications?cursor={tampered}")
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "INVALID_CURSOR"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    def test_unread_only_excludes_read_rows(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        a1 = _make_alert(repo, "p-1", severity="critical")
        a2 = _make_alert(repo, "p-1", severity="info")
        _make_alert(repo, "p-1", severity="critical")
        repo._alerts[a1]["read_at"] = datetime.now(timezone.utc)

        r = client.get("/api/v1/notifications?unread_only=true")
        body = r.json()
        ids = [n["id"] for n in body["notifications"]]
        assert a1 not in ids  # read
        assert a2 in ids  # unread
        assert len(ids) == 2

    def test_unread_only_false_includes_read_rows(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        a1 = _make_alert(repo, "p-1", severity="info")
        _make_alert(repo, "p-1", severity="info")
        repo._alerts[a1]["read_at"] = datetime.now(timezone.utc)
        r = client.get("/api/v1/notifications?unread_only=false")
        ids = [n["id"] for n in r.json()["notifications"]]
        assert len(ids) == 2

    def test_severity_filter_critical(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        _make_alert(repo, "p-1", severity="critical")
        _make_alert(repo, "p-1", severity="critical")
        _make_alert(repo, "p-1", severity="info")
        r = client.get("/api/v1/notifications?severity=critical")
        body = r.json()
        assert len(body["notifications"]) == 2
        for n in body["notifications"]:
            assert n["severity"] == "critical"

    def test_severity_filter_csv(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        _make_alert(repo, "p-1", severity="critical")
        _make_alert(repo, "p-1", severity="warning")
        _make_alert(repo, "p-1", severity="info")
        _make_alert(repo, "p-1", severity="urgent")
        r = client.get("/api/v1/notifications?severity=critical,warning")
        body = r.json()
        sevs = {n["severity"] for n in body["notifications"]}
        assert sevs == {"critical", "warning"}

    def test_invalid_severity_returns_400(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        r = client.get("/api/v1/notifications?severity=critical,banana")
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "INVALID_SEVERITY"

    def test_severity_case_insensitive(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        _make_alert(repo, "p-1", severity="critical")
        r = client.get("/api/v1/notifications?severity=CRITICAL")
        assert len(r.json()["notifications"]) == 1


# ---------------------------------------------------------------------------
# Mark read
# ---------------------------------------------------------------------------


class TestMarkRead:
    def test_mark_read_sets_read_at(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        a1 = _make_alert(repo, "p-1", severity="critical")
        r = client.post(f"/api/v1/notifications/{a1}/read")
        assert r.status_code == 200
        body = r.json()
        assert body == {"id": a1, "read": True}
        # Repo reflects the change.
        assert repo._alerts[a1]["read_at"] is not None

    def test_mark_read_unknown_id_returns_404(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        r = client.post("/api/v1/notifications/never-existed/read")
        assert r.status_code == 404
        body = r.json()["detail"]
        assert body["error"] == "NOTIFICATION_NOT_FOUND"

    def test_mark_read_other_parents_id_returns_404(
        self,
        client: TestClient,
        client_other_parent: TestClient,
        repo: InMemoryDashboardRepository,
    ) -> None:
        # Insert an alert that belongs to p-1.
        a1 = _make_alert(repo, "p-1", severity="critical")
        # p-other tries to mark it.
        r = client_other_parent.post(
            f"/api/v1/notifications/{a1}/read"
        )
        # Lateral-auth: 404 (not 403) so the existence of the id
        # doesn't leak to other parents.
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "NOTIFICATION_NOT_FOUND"
        # Repo untouched.
        assert repo._alerts[a1]["read_at"] is None

    def test_mark_read_is_idempotent(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        a1 = _make_alert(repo, "p-1", severity="critical")
        r1 = client.post(f"/api/v1/notifications/{a1}/read")
        r2 = client.post(f"/api/v1/notifications/{a1}/read")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == r2.json()


# ---------------------------------------------------------------------------
# Mark all read
# ---------------------------------------------------------------------------


class TestReadAll:
    def test_read_all_marks_every_unread(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        a1 = _make_alert(repo, "p-1", severity="critical")
        a2 = _make_alert(repo, "p-1", severity="info")
        a3 = _make_alert(repo, "p-1", severity="warning")
        # Already read — should NOT be re-counted.
        a4 = _make_alert(repo, "p-1", severity="info")
        repo._alerts[a4]["read_at"] = datetime.now(timezone.utc)
        # Different parent — must NOT be touched.
        _make_alert(repo, "p-other", severity="critical")

        r = client.post("/api/v1/notifications/read-all")
        assert r.status_code == 200
        body = r.json()
        assert body["marked"] == 3
        assert body["parent_id"] == "p-1"

        for aid in (a1, a2, a3):
            assert repo._alerts[aid]["read_at"] is not None
        # Other parent's row untouched.
        for row in repo._alerts.values():
            if row["parent_id"] == "p-other":
                assert row["read_at"] is None

    def test_read_all_idempotent_when_nothing_unread(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        a1 = _make_alert(repo, "p-1", severity="info")
        repo._alerts[a1]["read_at"] = datetime.now(timezone.utc)
        r = client.post("/api/v1/notifications/read-all")
        assert r.status_code == 200
        assert r.json()["marked"] == 0

    def test_read_all_empty_parent_returns_zero(
        self, client: TestClient, repo: InMemoryDashboardRepository
    ) -> None:
        r = client.post("/api/v1/notifications/read-all")
        assert r.status_code == 200
        assert r.json() == {"marked": 0, "parent_id": "p-1"}


# ---------------------------------------------------------------------------
# OpenAPI tag
# ---------------------------------------------------------------------------


class TestOpenAPITag:
    def test_notifications_tag_has_description(self) -> None:
        from niki.api.notifications import router

        tags = router.tags or []
        assert any(
            isinstance(t, dict)
            and t.get("name") == "notifications"
            and t.get("description")
            for t in tags
        ), f"notifications tag missing description: {tags!r}"

    def test_openapi_includes_notifications_endpoints(self) -> None:
        from niki.api.notifications import router as notif_router

        # 3 BE-PUSH-005 endpoints registered on the notifications
        # router. (The 4th — /dashboard/alerts/{child_id} — lives on
        # the dashboard router; we assert it's still present too.)
        paths = [r.path for r in notif_router.routes if hasattr(r, "path")]
        assert "/notifications" in paths
        assert "/notifications/{notification_id}/read" in paths
        assert "/notifications/read-all" in paths
        # Also verify the dashboard/alerts endpoint still exists on
        # the dashboard router (it predates this ticket — we just
        # rely on it for the per-child view).
        from niki.api.dashboard import router as dash_router
        dash_paths = [
            r.path for r in dash_router.routes if hasattr(r, "path")
        ]
        assert "/dashboard/alerts/{child_id}" in dash_paths