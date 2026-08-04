"""Tests for the unified audit log (BE-COMP-004).

Covers the three layers the ticket adds:
  1. `AuditLogService.log_event` — writes a row, scrubs PII.
  2. `GET /api/v1/admin/audit` — actor-scoped read + filters.
  3. Cross-domain wiring — consent / safety / high-risk / login
     events land in `audit_logs`, and `auth_audit_logs` still works
     (the dual-write must not have broken the lateral-auth path).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.models.audit_log import (
    EVENT_CONSENT_SIGNED,
    EVENT_HIGH_RISK_RECORDED,
    EVENT_LOGIN_SUCCESS,
    EVENT_SAFETY_BLOCK,
    EVENT_SMS_SENT,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
)
from niki.db.repositories import (
    AuditEvent,
    InMemoryAuditLogRepository,
    get_audit_log_repository,
    get_auth_audit_log_repository,
    set_audit_log_repository,
)
from niki.main import create_app
from niki.services.audit_log import (
    AuditLogService,
    get_audit_log_service,
    set_audit_log_service,
)
from niki.services.child_registry import (
    InMemoryChildRegistry,
    set_child_registry,
)

ACTOR = "p_audit"


@pytest.fixture
def repo() -> InMemoryAuditLogRepository:
    """A fresh unified-audit repo wired into the process singleton."""
    r = InMemoryAuditLogRepository()
    set_audit_log_repository(r)
    set_audit_log_service(AuditLogService(repo=r))
    return r


@pytest.fixture
def service(repo: InMemoryAuditLogRepository) -> AuditLogService:
    return get_audit_log_service()


@pytest.fixture
def client(
    repo: InMemoryAuditLogRepository, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """TestClient bound to parent id `p_audit`.

    Overrides `get_current_parent` directly (rather than minting a
    JWT) so audit rows have a deterministic actor to assert on.
    """
    app = create_app()

    def fake() -> ParentAccount:
        return ParentAccount(
            id=ACTOR,
            name="Audit Parent",
            id_number_masked="****",
            phone_masked="****",
        )

    app.dependency_overrides[get_current_parent] = fake
    return TestClient(app)


# ---------------------------------------------------------------------------
# AC-1: log_event creates a row
# ---------------------------------------------------------------------------


class TestLogEventCreatesRow:
    """`AuditLogService.log_event` persists exactly one row."""

    @pytest.mark.asyncio
    async def test_log_event_creates_row(
        self, service: AuditLogService, repo: InMemoryAuditLogRepository
    ) -> None:
        row_id = await service.log_event(
            EVENT_CONSENT_SIGNED,
            actor_parent_id=ACTOR,
            target_parent_id=ACTOR,
            endpoint="/api/v1/auth/signup/consent",
            method="POST",
            reason="signed",
            metadata={"doc_version": "v1"},
            client_ip="203.0.113.7",
        )
        assert row_id is not None
        assert len(repo) == 1

        rows = await repo.list_events()
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == row_id
        assert row["event_type"] == EVENT_CONSENT_SIGNED
        assert row["severity"] == "info"
        assert row["actor_parent_id"] == ACTOR
        assert row["endpoint"] == "/api/v1/auth/signup/consent"
        assert row["method"] == "POST"
        assert row["metadata"] == {"doc_version": "v1"}
        assert row["client_ip"] == "203.0.113.7"
        assert isinstance(row["created_at"], datetime)

    @pytest.mark.asyncio
    async def test_severity_is_recorded(
        self, service: AuditLogService, repo: InMemoryAuditLogRepository
    ) -> None:
        """Non-info severities round-trip (they drive alert routing)."""
        await service.log_event(
            EVENT_SAFETY_BLOCK,
            severity=SEVERITY_WARNING,
            actor_parent_id=ACTOR,
        )
        await service.log_event(
            EVENT_HIGH_RISK_RECORDED,
            severity=SEVERITY_CRITICAL,
            actor_parent_id=ACTOR,
        )
        rows = await repo.list_events()
        by_type = {r["event_type"]: r for r in rows}
        assert by_type[EVENT_SAFETY_BLOCK]["severity"] == SEVERITY_WARNING
        assert (
            by_type[EVENT_HIGH_RISK_RECORDED]["severity"]
            == SEVERITY_CRITICAL
        )

    @pytest.mark.asyncio
    async def test_bad_severity_is_swallowed_not_raised(
        self, service: AuditLogService, repo: InMemoryAuditLogRepository
    ) -> None:
        """An audit bug must never 500 the caller's own request."""
        row_id = await service.log_event(
            EVENT_SAFETY_BLOCK,
            severity="not_a_severity",
            actor_parent_id=ACTOR,
        )
        assert row_id is None
        assert len(repo) == 0

    @pytest.mark.asyncio
    async def test_pii_metadata_is_redacted(
        self, service: AuditLogService, repo: InMemoryAuditLogRepository
    ) -> None:
        """The unified table must not accumulate child text / phones."""
        await service.log_event(
            EVENT_HIGH_RISK_RECORDED,
            actor_parent_id=ACTOR,
            metadata={
                "original_text": "我不想活了",
                "phone": "13800138000",
                "round_reached": 4,
            },
        )
        row = (await repo.list_events())[0]
        assert row["metadata"]["original_text"] == "[redacted]"
        assert row["metadata"]["phone"] == "[redacted]"
        # Non-PII keys survive untouched.
        assert row["metadata"]["round_reached"] == 4

    @pytest.mark.asyncio
    async def test_long_metadata_string_is_truncated(
        self, service: AuditLogService, repo: InMemoryAuditLogRepository
    ) -> None:
        """Long free text is truncated even under an allowed key."""
        await service.log_event(
            EVENT_SAFETY_BLOCK,
            actor_parent_id=ACTOR,
            metadata={"note": "x" * 500},
        )
        row = (await repo.list_events())[0]
        assert len(row["metadata"]["note"]) < 500
        assert row["metadata"]["note"].endswith("…")


# ---------------------------------------------------------------------------
# AC-2: GET /admin/audit returns scoped rows
# ---------------------------------------------------------------------------


class TestAdminAuditEndpoint:
    """`GET /api/v1/admin/audit` reads back the caller's own rows."""

    def test_returns_scoped_rows(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        service = get_audit_log_service()
        import asyncio

        async def seed() -> None:
            await service.log_event(
                EVENT_CONSENT_SIGNED, actor_parent_id=ACTOR
            )
            await service.log_event(
                EVENT_SAFETY_BLOCK,
                severity=SEVERITY_WARNING,
                actor_parent_id=ACTOR,
            )
            # Another parent's row — must NOT leak into the response.
            await service.log_event(
                EVENT_CONSENT_SIGNED, actor_parent_id="p_other"
            )

        asyncio.run(seed())

        resp = client.get("/api/v1/admin/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["actor_parent_id"] == ACTOR
        assert body["count"] == 2
        actors = {e["actor_parent_id"] for e in body["events"]}
        assert actors == {ACTOR}
        # Newest-first.
        assert body["events"][0]["event_type"] == EVENT_SAFETY_BLOCK
        # created_at is serialised to an ISO string, not a datetime.
        assert isinstance(body["events"][0]["created_at"], str)

    def test_empty_history_is_200_not_404(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        resp = client.get("/api/v1/admin/audit")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0
        assert resp.json()["events"] == []

    def test_other_parents_id_is_403(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        """v1 scoping: asking for someone else's history is refused."""
        resp = client.get(
            "/api/v1/admin/audit", params={"actor_parent_id": "p_other"}
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    def test_own_id_is_allowed_explicitly(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        resp = client.get(
            "/api/v1/admin/audit", params={"actor_parent_id": ACTOR}
        )
        assert resp.status_code == 200

    def test_limit_is_capped(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        resp = client.get("/api/v1/admin/audit", params={"limit": 5000})
        assert resp.status_code == 400

    def test_limit_applies(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        import asyncio

        service = get_audit_log_service()

        async def seed() -> None:
            for _ in range(5):
                await service.log_event(
                    EVENT_SAFETY_BLOCK, actor_parent_id=ACTOR
                )

        asyncio.run(seed())
        resp = client.get("/api/v1/admin/audit", params={"limit": 2})
        assert resp.status_code == 200
        assert resp.json()["count"] == 2


# ---------------------------------------------------------------------------
# AC-3: ?event_type= filters
# ---------------------------------------------------------------------------


class TestEventTypeFilter:
    """`?event_type=` narrows to a single event class."""

    def test_event_type_filters(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        import asyncio

        service = get_audit_log_service()

        async def seed() -> None:
            await service.log_event(
                EVENT_CONSENT_SIGNED, actor_parent_id=ACTOR
            )
            await service.log_event(
                EVENT_SAFETY_BLOCK, actor_parent_id=ACTOR
            )
            await service.log_event(
                EVENT_SAFETY_BLOCK, actor_parent_id=ACTOR
            )

        asyncio.run(seed())

        resp = client.get(
            "/api/v1/admin/audit",
            params={"event_type": EVENT_SAFETY_BLOCK},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["event_type_filter"] == EVENT_SAFETY_BLOCK
        assert body["count"] == 2
        assert {e["event_type"] for e in body["events"]} == {
            EVENT_SAFETY_BLOCK
        }

    def test_unknown_event_type_returns_empty(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        import asyncio

        service = get_audit_log_service()
        asyncio.run(
            service.log_event(EVENT_CONSENT_SIGNED, actor_parent_id=ACTOR)
        )
        resp = client.get(
            "/api/v1/admin/audit",
            params={"event_type": "no_such_event"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


# ---------------------------------------------------------------------------
# AC-4: ?since= filters
# ---------------------------------------------------------------------------


class TestSinceFilter:
    """`?since=` bounds the time window (inclusive lower bound)."""

    def test_since_in_the_future_excludes_everything(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        import asyncio

        service = get_audit_log_service()
        asyncio.run(
            service.log_event(EVENT_CONSENT_SIGNED, actor_parent_id=ACTOR)
        )

        future = datetime.now(timezone.utc) + timedelta(days=1)
        resp = client.get(
            "/api/v1/admin/audit", params={"since": future.isoformat()}
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_since_in_the_past_includes_everything(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        import asyncio

        service = get_audit_log_service()
        asyncio.run(
            service.log_event(EVENT_CONSENT_SIGNED, actor_parent_id=ACTOR)
        )

        past = datetime.now(timezone.utc) - timedelta(days=1)
        resp = client.get(
            "/api/v1/admin/audit", params={"since": past.isoformat()}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["since"] is not None

    def test_since_accepts_date_only(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        """`YYYY-MM-DD` is the shape the frontend date-picker emits."""
        import asyncio

        service = get_audit_log_service()
        asyncio.run(
            service.log_event(EVENT_CONSENT_SIGNED, actor_parent_id=ACTOR)
        )
        resp = client.get(
            "/api/v1/admin/audit", params={"since": "2020-01-01"}
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    @pytest.mark.asyncio
    async def test_repo_since_boundary_is_inclusive(
        self, service: AuditLogService, repo: InMemoryAuditLogRepository
    ) -> None:
        await service.log_event(
            EVENT_CONSENT_SIGNED, actor_parent_id=ACTOR
        )
        row = (await repo.list_events())[0]
        # since == the row's own timestamp keeps the row.
        assert len(await repo.list_events(since=row["created_at"])) == 1
        # One microsecond later drops it.
        later = row["created_at"] + timedelta(microseconds=1)
        assert len(await repo.list_events(since=later)) == 0


# ---------------------------------------------------------------------------
# AC-5: cross-domain call sites write to the unified table
# ---------------------------------------------------------------------------


class TestCrossDomainWiring:
    """Real endpoints funnel through the unified table."""

    def test_safety_valve_logs_safety_block(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        set_child_registry(_registry_with(ACTOR, "child-audit-1"))
        resp = client.post(
            "/api/v1/dashboard/alerts/record/safety-valve",
            json={
                "child_id": "child-audit-1",
                "problem_summary": "3 位数乘法卡住了",
                "round_reached": 4,
            },
        )
        assert resp.status_code == 200

        audit = client.get(
            "/api/v1/admin/audit",
            params={"event_type": EVENT_SAFETY_BLOCK},
        ).json()
        assert audit["count"] == 1
        row = audit["events"][0]
        assert row["severity"] == SEVERITY_WARNING
        assert row["target_child_id"] == "child-audit-1"
        assert row["metadata"]["round_reached"] == 4
        # The problem summary is child-adjacent text — not here.
        assert "problem_summary" not in row["metadata"]

    def test_high_risk_logs_critical_without_child_text(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        set_child_registry(_registry_with(ACTOR, "child-audit-2"))
        resp = client.post(
            "/api/v1/dashboard/alerts/record/high-risk",
            json={
                "child_id": "child-audit-2",
                "original_text": "我不想活了",
            },
        )
        assert resp.status_code == 200

        audit = client.get(
            "/api/v1/admin/audit",
            params={"event_type": EVENT_HIGH_RISK_RECORDED},
        ).json()
        assert audit["count"] == 1
        row = audit["events"][0]
        assert row["severity"] == SEVERITY_CRITICAL
        assert row["target_child_id"] == "child-audit-2"
        # PIPL: the child's own words never reach `audit_logs`.
        serialised = str(row)
        assert "我不想活了" not in serialised

    def test_consent_signature_is_audited(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        """Full signup → consent flow lands a `consent_signed` row."""
        start = client.post(
            "/api/v1/auth/signup/start",
            json={
                "name": "审计家长",
                "id_number": "110101199003078811",
                "phone": "13800138111",
            },
        )
        assert start.status_code == 200
        parent_id = start.json()["account"]["id"]

        for step, payload in (
            (
                "real-name",
                {
                    "parent_id": parent_id,
                    "name": "审计家长",
                    "id_number": "110101199003078811",
                },
            ),
            (
                "face",
                {
                    "parent_id": parent_id,
                    "live_image_url": "https://example.test/selfie.jpg",
                },
            ),
        ):
            step_resp = client.post(
                f"/api/v1/auth/signup/{step}", json=payload
            )
            assert step_resp.status_code == 200, step_resp.text

        consent = client.post(
            "/api/v1/auth/signup/consent",
            json={
                "parent_id": parent_id,
                "typed_signature": "审计家长",
                "sign_child_consent": True,
            },
        )
        assert consent.status_code == 200

        # The row is scoped to the *signing* parent, so read the repo
        # directly rather than through the ACTOR-scoped endpoint.
        import asyncio

        rows = asyncio.run(
            repo.list_events(event_type=EVENT_CONSENT_SIGNED)
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["actor_parent_id"] == parent_id
        assert row["metadata"]["signed_child_consent"] is True
        # The signature itself must never be persisted here.
        assert "审计家长" not in str(row["metadata"])

    def test_sms_send_and_login_are_audited(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        import asyncio

        send = client.post(
            "/api/v1/auth/sms/send", json={"phone": "13800138222"}
        )
        assert send.status_code == 200

        sms_rows = asyncio.run(repo.list_events(event_type=EVENT_SMS_SENT))
        assert len(sms_rows) == 1
        # Pre-auth event: no actor, and definitely no phone number.
        assert sms_rows[0]["actor_parent_id"] is None
        assert "13800138222" not in str(sms_rows[0])

        code = send.json().get("dev_code")
        if code is None:
            pytest.skip("dev_code not echoed (debug off); login untestable")

        login = client.post(
            "/api/v1/auth/sms/login",
            json={"phone": "13800138222", "code": code},
        )
        assert login.status_code == 200

        login_rows = asyncio.run(
            repo.list_events(event_type=EVENT_LOGIN_SUCCESS)
        )
        assert len(login_rows) == 1
        assert login_rows[0]["actor_parent_id"] == login.json()["parent_id"]


# ---------------------------------------------------------------------------
# AC-6: existing auth_audit_logs still works (no break)
# ---------------------------------------------------------------------------


class TestAuthAuditLogsUnbroken:
    """The dual-write must not disturb the lateral-auth trail."""

    @pytest.mark.asyncio
    async def test_lateral_violation_writes_both_tables(
        self, repo: InMemoryAuditLogRepository
    ) -> None:
        from niki.services.auth_audit import get_auth_audit_logger

        auth_repo = get_auth_audit_log_repository()
        row_id = await get_auth_audit_logger().log_violation(
            "lateral_auth_violation",
            actor_parent_id=ACTOR,
            reason="parent/child mismatch",
            endpoint="/api/v1/dashboard/alerts/child-x",
            method="GET",
            target_child_id="child-x",
        )

        # 1. The original narrow table still gets its row.
        assert row_id is not None
        auth_rows = await auth_repo.list_recent(actor_parent_id=ACTOR)
        assert len(auth_rows) == 1
        assert auth_rows[0]["event_type"] == "lateral_auth_violation"
        assert auth_rows[0]["severity"] == SEVERITY_WARNING
        # No `metadata` column on the legacy table — its shape is
        # unchanged by this ticket.
        assert "metadata" not in auth_rows[0]

        # 2. And the unified table mirrors it for the cross-domain view.
        unified = await repo.list_events(
            event_type="lateral_auth_violation"
        )
        assert len(unified) == 1
        assert unified[0]["actor_parent_id"] == ACTOR
        assert unified[0]["target_child_id"] == "child-x"
        assert unified[0]["metadata"]["source"] == "auth_audit_logs"

    def test_legacy_endpoint_still_serves_its_own_table(
        self, client: TestClient, repo: InMemoryAuditLogRepository
    ) -> None:
        """`/auth/audit/recent` reads `auth_audit_logs`, not the new one."""
        import asyncio

        auth_repo = get_auth_audit_log_repository()
        asyncio.run(
            auth_repo.add(
                AuthAuditEventFactory(actor_parent_id=ACTOR)
            )
        )
        # A unified-only row must NOT show up on the legacy endpoint.
        asyncio.run(
            get_audit_log_service().log_event(
                EVENT_CONSENT_SIGNED, actor_parent_id=ACTOR
            )
        )

        resp = client.get("/api/v1/auth/audit/recent")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["event_type"] == "lateral_auth_violation"


def AuthAuditEventFactory(*, actor_parent_id: str):
    """Build a minimal legacy `AuthAuditEvent` for the no-break test."""
    from niki.db.repositories import AuthAuditEvent

    return AuthAuditEvent.violation(
        "lateral_auth_violation",
        actor_parent_id=actor_parent_id,
        reason="parent/child mismatch",
        endpoint="/api/v1/dashboard/alerts/child-x",
        method="GET",
        target_child_id="child-x",
    )


# ---------------------------------------------------------------------------
# Repository contract
# ---------------------------------------------------------------------------


class TestRepositoryContract:
    """Direct repo assertions the API tests can't reach."""

    @pytest.mark.asyncio
    async def test_count_respects_filters(
        self, repo: InMemoryAuditLogRepository
    ) -> None:
        await repo.add(
            AuditEvent(EVENT_SAFETY_BLOCK, actor_parent_id=ACTOR)
        )
        await repo.add(
            AuditEvent(EVENT_SAFETY_BLOCK, actor_parent_id="p_other")
        )
        await repo.add(
            AuditEvent(EVENT_CONSENT_SIGNED, actor_parent_id=ACTOR)
        )

        assert await repo.count() == 3
        assert await repo.count(event_type=EVENT_SAFETY_BLOCK) == 2
        assert await repo.count(actor_parent_id=ACTOR) == 2
        assert (
            await repo.count(
                event_type=EVENT_SAFETY_BLOCK, actor_parent_id=ACTOR
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_list_is_insertion_ordered_newest_first(
        self, repo: InMemoryAuditLogRepository
    ) -> None:
        """Back-to-back writes still come back in insertion order."""
        for i in range(5):
            await repo.add(
                AuditEvent(
                    EVENT_SAFETY_BLOCK,
                    actor_parent_id=ACTOR,
                    reason=str(i),
                )
            )
        rows = await repo.list_events()
        assert [r["reason"] for r in rows] == ["4", "3", "2", "1", "0"]

    @pytest.mark.asyncio
    async def test_internal_seq_is_not_leaked(
        self, repo: InMemoryAuditLogRepository
    ) -> None:
        await repo.add(AuditEvent(EVENT_SAFETY_BLOCK))
        assert "_seq" not in (await repo.list_events())[0]

    def test_event_rejects_empty_event_type(self) -> None:
        with pytest.raises(ValueError, match="event_type"):
            AuditEvent("")

    def test_event_rejects_bad_severity(self) -> None:
        with pytest.raises(ValueError, match="severity"):
            AuditEvent(EVENT_SAFETY_BLOCK, severity="loud")

    def test_singleton_default_is_in_memory(self) -> None:
        set_audit_log_repository(None)  # type: ignore[arg-type]
        assert isinstance(
            get_audit_log_repository(), InMemoryAuditLogRepository
        )


# ---------------------------------------------------------------------------
# Model shape
# ---------------------------------------------------------------------------


class TestModelShape:
    """The table carries the columns + composite indexes the ticket asks for."""

    def test_columns(self) -> None:
        from niki.db.base import Base

        cols = {c.name for c in Base.metadata.tables["audit_logs"].columns}
        assert cols == {
            "id",
            "event_type",
            "severity",
            "actor_parent_id",
            "target_parent_id",
            "target_child_id",
            "endpoint",
            "method",
            "reason",
            "metadata",
            "client_ip",
            "created_at",
        }

    def test_composite_indexes(self) -> None:
        from niki.db.base import Base

        names = {
            i.name for i in Base.metadata.tables["audit_logs"].indexes
        }
        assert "ix_audit_log_actor_time" in names
        assert "ix_audit_log_event_time" in names

    def test_auth_audit_logs_table_still_present(self) -> None:
        """This ticket ADDS a table; it does not remove the old one."""
        from niki.db.base import Base

        assert "auth_audit_logs" in Base.metadata.tables
        assert "audit_logs" in Base.metadata.tables


def _registry_with(parent_id: str, *child_ids: str) -> InMemoryChildRegistry:
    registry = InMemoryChildRegistry()
    for child_id in child_ids:
        registry.register(child_id, parent_id)
    return registry
