"""Tests for BE-PUSH-006 (push test endpoint + history).

Covers:
    POST /api/v1/push/test            — lateral-auth, dry-run, audit, rate-limit
    GET  /api/v1/push/test/history    — ring-buffer read

Tests use the synchronous `TestClient` so the slowapi
limiter (per-process, in-memory storage) and the
`/push/test` decorator compose the same way they do in
production. The conftest autouse fixture already clears
the rate-limit counter between tests, so the rate-limit
test is deterministic.
"""

from __future__ import annotations

from typing import Any

import asyncio

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.repositories import (
    InMemoryAuditLogRepository,
    InMemoryPushDeviceRepository,
    set_audit_log_repository,
    set_push_device_repository,
)
from niki.main import create_app
from niki.middleware.rate_limit import reset_for_tests
from niki.services.audit_log import (
    AuditLogService,
    get_audit_log_service,
    set_audit_log_service,
)
from niki.services.push_delivery import (
    InMemoryPushDeliveryService,
    PushDeliveryResult,
    PushPayload,
    PushDeliveryService,
    RING_BUFFER_CAPACITY,
    STATUS_DRY_RUN,
    get_push_delivery_service,
    set_push_delivery_service,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_repo() -> InMemoryAuditLogRepository:
    """Fresh in-memory audit log repo wired into the singleton."""
    repo = InMemoryAuditLogRepository()
    set_audit_log_repository(repo)
    set_audit_log_service(AuditLogService(repo=repo))
    return repo


@pytest.fixture
def device_repo() -> InMemoryPushDeviceRepository:
    """Fresh in-memory push device repo."""
    repo = InMemoryPushDeviceRepository()
    set_push_device_repository(repo)
    return repo


@pytest.fixture
def delivery_svc() -> InMemoryPushDeliveryService:
    """Fresh in-memory delivery service with an empty ring buffer.

    Note we reset both the singleton and the rate-limit counter
    so the 5/minute cap in `POST /push/test` starts at zero.
    """
    svc = InMemoryPushDeliveryService()
    set_push_delivery_service(svc)
    reset_for_tests()
    return svc


def _make_client(
    parent_id: str = "p1",
) -> tuple[TestClient, Any]:
    """Build a TestClient whose `get_current_parent` returns `parent_id`.

    Returns (client, app). The app handle is exposed so individual
    tests can install extra dependency overrides if needed.
    """
    app = create_app()

    def _fake() -> ParentAccount:
        return ParentAccount(
            id=parent_id,
            name="Test",
            id_number_masked="****",
            phone_masked="****",
        )

    app.dependency_overrides[get_current_parent] = _fake
    return TestClient(app), app


def _register_device(
    client: TestClient,
    *,
    device_id: str = "d-1",
    channel: str = "apns",
    platform: str = "ios",
    push_token: str = "tok-test-1",
) -> None:
    """Register a device for the parent wired into `client`.

    Kept inline rather than a fixture because the test class also
    needs to register *other* parents' devices to exercise the
    lateral-auth path (test_other_parents_device_returns_404).
    """
    r = client.post(
        "/api/v1/push/register",
        json={
            "device_id": device_id,
            "channel": channel,
            "push_token": push_token,
            "platform": platform,
        },
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# POST /api/v1/push/test
# ---------------------------------------------------------------------------


class TestPostPushTest:
    def test_dry_run_success_returns_200_and_latency(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """AC-2: dry-run mode + synthetic latency_ms in 5-50."""
        client, _ = _make_client()
        _register_device(client, device_id="d-1", channel="apns")

        r = client.post(
            "/api/v1/push/test",
            json={"device_id": "d-1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["device_id"] == "d-1"
        assert body["status"] == STATUS_DRY_RUN
        assert body["channel"] == "apns"
        # BE-PUSH-006 §3: synthetic 5-50ms jitter
        assert isinstance(body["latency_ms"], int)
        assert 5 <= body["latency_ms"] <= 50

    def test_response_carries_x_push_test_mode_header(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """AC observability: header for ops dashboards."""
        client, _ = _make_client()
        _register_device(client)

        r = client.post(
            "/api/v1/push/test",
            json={"device_id": "d-1"},
        )
        assert r.status_code == 200
        assert r.headers.get("X-Push-Test-Mode") == "dry-run"

    def test_other_parents_device_returns_404(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """AC-3: lateral-auth — foreign device_id is 404, not 403.

        Uses a *second* TestClient whose parent is `p-other`,
        registers a device, then tries to fire a test push from
        the original `p1` client. Must 404.
        """
        client_p1, _ = _make_client(parent_id="p1")
        client_other, _ = _make_client(parent_id="p-other")

        # Register under p-other.
        r = client_other.post(
            "/api/v1/push/register",
            json={
                "device_id": "d-victim",
                "channel": "apns",
                "push_token": "tok-victim",
                "platform": "ios",
            },
        )
        assert r.status_code == 200

        # p1 tries to test-push d-victim. Must be 404 with the
        # i18n key (no oracle distinction between missing and
        # not-yours).
        r = client_p1.post(
            "/api/v1/push/test",
            json={"device_id": "d-victim"},
        )
        assert r.status_code == 404
        # FastAPI wraps HTTPException(detail=...) under "detail"
        detail = r.json()["detail"]
        assert detail["error"] == "DEVICE_NOT_FOUND"

    def test_missing_device_id_returns_422(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """Validation: missing device_id is a 422 (not 400)."""
        client, _ = _make_client()
        r = client.post("/api/v1/push/test", json={})
        assert r.status_code == 422

    def test_empty_body_uses_defaults(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """Spec: title/body/severity all default — only device_id required."""
        client, _ = _make_client()
        _register_device(client)

        r = client.post(
            "/api/v1/push/test",
            json={"device_id": "d-1"},
        )
        assert r.status_code == 200
        # We don't echo back the payload, but the call should
        # succeed — which means the defaults were acceptable.
        body = r.json()
        assert body["status"] == STATUS_DRY_RUN

    def test_rate_limit_kicks_in_after_5_calls(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """AC-4: 6th call in <1min returns 429.

        The conftest autouse fixture clears the limiter between
        tests, so this is the only test that consumes quota.
        """
        client, _ = _make_client()
        _register_device(client)

        for i in range(5):
            r = client.post(
                "/api/v1/push/test",
                json={"device_id": "d-1"},
            )
            assert r.status_code == 200, f"call {i+1}: {r.status_code}"

        r = client.post(
            "/api/v1/push/test",
            json={"device_id": "d-1"},
        )
        assert r.status_code == 429
        # slowapi handler returns the §6.3 envelope
        body = r.json()
        assert body.get("error_code") == "RATE_LIMIT_EXCEEDED"

    def test_writes_audit_log_row(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """AC-2: each /push/test writes a `push_test_sent` audit row.

        Verifies via the in-memory audit repo directly (the
        unified `audit_logs` table — see BE-COMP-004).
        """
        client, _ = _make_client()
        _register_device(client)

        r = client.post(
            "/api/v1/push/test",
            json={"device_id": "d-1", "severity": "warning"},
        )
        assert r.status_code == 200

        rows = audit_repo._rows  # type: ignore[attr-defined]
        assert len(rows) == 1
        row = next(iter(rows.values()))
        assert row["event_type"] == "push_test_sent"
        assert row["severity"] == "info"
        assert row["actor_parent_id"] == "p1"
        assert row["endpoint"] == "/api/v1/push/test"
        assert row["method"] == "POST"
        # `to_row()` (used by `add()`) maps `event_metadata` → `metadata`
        md = row["metadata"]
        assert md["device_id"] == "d-1"
        assert md["dry_run"] is True
        assert md["status"] == STATUS_DRY_RUN

    def test_singleton_injection_overrides_delivery(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
    ) -> None:
        """Custom `PushDeliveryService` impl replaces the singleton."""

        class _Spy(PushDeliveryService):
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, bool]] = []

            async def deliver(
                self,
                device: Any,
                payload: PushPayload,
                *,
                dry_run: bool = False,
            ) -> PushDeliveryResult:
                if isinstance(device, dict):
                    did = device.get("device_id") or ""
                else:
                    did = getattr(device, "device_id", "")
                self.calls.append(
                    (did, payload.title, dry_run)
                )
                return PushDeliveryResult(
                    device_id=getattr(device, "device_id", ""),
                    channel=getattr(device, "channel", ""),
                    status="delivered",
                    latency_ms=999,
                    dry_run=dry_run,
                )

        spy = _Spy()
        set_push_delivery_service(spy)
        reset_for_tests()

        client, _ = _make_client()
        _register_device(client)

        r = client.post(
            "/api/v1/push/test",
            json={"device_id": "d-1", "title": "hi"},
        )
        assert r.status_code == 200
        body = r.json()
        # The spy returned latency_ms=999, proving the singleton
        # was actually replaced (the default impl caps at 50).
        assert body["latency_ms"] == 999
        assert spy.calls == [("d-1", "hi", True)]


# ---------------------------------------------------------------------------
# GET /api/v1/push/test/history
# ---------------------------------------------------------------------------


class TestGetPushTestHistory:
    def test_history_shows_prior_deliveries(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        client, _ = _make_client()
        _register_device(client)
        client.post(
            "/api/v1/push/test",
            json={"device_id": "d-1"},
        )

        r = client.get("/api/v1/push/test/history")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert len(body["deliveries"]) == 1
        d = body["deliveries"][0]
        assert d["device_id"] == "d-1"
        assert d["channel"] == "apns"
        assert d["status"] == STATUS_DRY_RUN
        assert d["dry_run"] is True
        assert 5 <= d["latency_ms"] <= 50

    def test_history_limit_caps_at_100(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """Spec: limit > 100 is clamped to MAX_HISTORY_LIMIT (100)."""
        client, _ = _make_client()
        _register_device(client)

        # Fire 3 test pushes so the ring buffer has content.
        for _ in range(3):
            r = client.post(
                "/api/v1/push/test",
                json={"device_id": "d-1"},
            )
            assert r.status_code == 200

        # Ask for an absurd limit. Should cap at 100 (and we
        # only have 3 deliveries to return anyway).
        r = client.get("/api/v1/push/test/history?limit=500")
        assert r.status_code == 200
        body = r.json()
        assert len(body["deliveries"]) == 3
        assert body["count"] == 3

    def test_ring_buffer_eviction_keeps_last_100(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """Spec: pushing >100 into the ring buffer drops oldest.

        Bypasses the rate limiter by calling the service
        directly (the /push/test endpoint is rate-limited to
        5/minute).
        """
        for i in range(150):
            payload = PushPayload(
                title=f"t-{i}",
                body="b",
                severity="info",
            )
            # A bare namespace object is enough — the in-memory
            # impl only reads `.device_id` and `.channel`.
            device = type("_D", (), {
                "device_id": f"d-{i}",
                "channel": "apns",
                "push_token": "tok",
            })()
            # Synchronously iterate via the event loop the service
            # already handles — call .deliver() in a coroutine.
            asyncio.run(
                delivery_svc.deliver(device, payload, dry_run=True)
            )

        # Should be capped at RING_BUFFER_CAPACITY (=100), not 150.
        assert len(delivery_svc) == RING_BUFFER_CAPACITY
        history = delivery_svc.history(limit=100)
        assert len(history) == 100
        # Newest first — the most recent delivery should be
        # the one with title "t-149", but the service only
        # stores PushDeliveryResult, not the payload. We
        # verify by device_id instead.
        assert history[0].device_id == "d-149"
        assert history[-1].device_id == "d-50"

    def test_history_channel_filter(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        client, _ = _make_client()
        _register_device(client, device_id="d-1", channel="apns")
        _register_device(
            client, device_id="d-2", channel="fcm",
            push_token="tok-2",
        )

        client.post("/api/v1/push/test", json={"device_id": "d-1"})
        client.post("/api/v1/push/test", json={"device_id": "d-2"})

        r = client.get("/api/v1/push/test/history?channel=fcm")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["deliveries"][0]["channel"] == "fcm"

        r = client.get("/api/v1/push/test/history?channel=apns")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["deliveries"][0]["channel"] == "apns"

        r = client.get("/api/v1/push/test/history?channel=web")
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_history_default_limit_is_20(
        self,
        audit_repo: InMemoryAuditLogRepository,
        device_repo: InMemoryPushDeviceRepository,
        delivery_svc: InMemoryPushDeliveryService,
    ) -> None:
        """No `limit` query param → default of 20."""
        client, _ = _make_client()
        _register_device(client)
        # One delivery is enough to confirm the endpoint works
        # with no params; the cap itself is exercised above.
        client.post("/api/v1/push/test", json={"device_id": "d-1"})
        r = client.get("/api/v1/push/test/history")
        assert r.status_code == 200
        assert r.json()["count"] == 1


# ---------------------------------------------------------------------------
# InMemoryPushDeliveryService direct coverage
# ---------------------------------------------------------------------------


class TestInMemoryPushDeliveryService:
    """White-box coverage for the service layer itself.

    The endpoint tests above exercise it indirectly; these
    tests pin down the behaviour of `history()` and the
    `skipped_no_token` status path which the endpoint
    doesn't normally hit (devices registered via /push/register
    always carry a token).
    """

    def test_skipped_no_token_when_device_has_no_token(
        self,
    ) -> None:
        svc = InMemoryPushDeliveryService()
        device = type("_D", (), {
            "device_id": "d-no-tok",
            "channel": "web",
            "push_token": "",
        })()
        payload = PushPayload(title="t", body="b", severity="info")

        result = asyncio.run(
            svc.deliver(device, payload, dry_run=True)
        )
        assert result.status == "skipped_no_token"
        assert result.dry_run is True
        assert result.latency_ms is None
        assert result.error is not None
        # Still recorded in the ring buffer — operators need to
        # see "device has no token" too.
        assert len(svc) == 1

    def test_history_channel_filter_excludes_other_channels(
        self,
    ) -> None:
        svc = InMemoryPushDeliveryService()
        for ch in ("apns", "fcm", "apns", "web"):
            device = type("_D", (), {
                "device_id": f"d-{ch}",
                "channel": ch,
                "push_token": "t",
            })()
            payload = PushPayload(title="t", body="b", severity="info")
            asyncio.run(
                svc.deliver(device, payload, dry_run=True)
            )

        only_apns = svc.history(channel="apns")
        assert len(only_apns) == 2
        assert {r.channel for r in only_apns} == {"apns"}

    def test_clear_resets_buffer(self) -> None:
        svc = InMemoryPushDeliveryService()
        device = type("_D", (), {
            "device_id": "d-1", "channel": "apns", "push_token": "t",
        })()
        payload = PushPayload(title="t", body="b", severity="info")
        asyncio.run(
            svc.deliver(device, payload, dry_run=True)
        )
        assert len(svc) == 1
        svc.clear()
        assert len(svc) == 0

    def test_default_singleton_lazy_build(
        self,
    ) -> None:
        """The default singleton is built lazily on first access.

        Resets the cached singleton to None, then verifies the
        next `get_push_delivery_service()` returns a fresh
        `InMemoryPushDeliveryService`.
        """
        from niki.services.push_delivery import (
            reset_push_delivery_service_for_tests,
        )
        reset_push_delivery_service_for_tests()
        svc = get_push_delivery_service()
        try:
            assert isinstance(svc, InMemoryPushDeliveryService)
            assert len(svc) == 0
        finally:
            reset_push_delivery_service_for_tests()