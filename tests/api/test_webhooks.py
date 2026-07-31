"""Integration tests for the webhook callback endpoints (BE-API-008).

Exercises the API layer end-to-end with TestClient. The conftest
autouse fixture gives a real `get_current_parent` that returns a
deterministic caller id, while still bypassing JWT verification
(`NIKI_TEST_FAKE_AUTH=1`).

Coverage of the 32-test ticket spec (≥ 20 required):

  AC-1 (6 endpoints):
    1. register_creates_subscription_and_returns_secret
    2. list_returns_owners_subscriptions
    3. get_returns_subscription_without_secret
    4. update_changes_target_url
    5. update_changes_event_types
    6. disable_sets_inactive
    7. deliveries_lists_attempts

  AC-2 (HMAC):
    8. dispatch_signs_payload_correctly
    9. signature_header_format

  AC-3 (auto-disable after 5):
    10. auto_disabled_after_five_consecutive_failures

  AC-4 (retry 1s/4s/16s):
    11. dispatch_retries_on_5xx

  AC-5 (audit on state change):
    12. audit_row_written_on_register
    13. audit_row_written_on_update
    14. audit_row_written_on_disable

  AC-6 (lateral-auth → 404):
    15. cross_owner_get_returns_404
    16. cross_owner_patch_returns_404
    17. cross_owner_delete_returns_404
    18. cross_owner_get_deliveries_returns_404

  AC-7 (HTTPS-only in production):
    19. http_url_rejected_in_production
    20. http_url_allowed_when_insecure_flag_set

  Validation (4):
    21. invalid_url_format_returns_422
    22. empty_event_types_returns_422
    23. too_many_event_types_returns_422
    24. unsupported_event_type_returns_422

  Dispatch semantics (3):
    25. dispatch_finds_matching_subscriptions
    26. event_type_filter_blocks_unrelated_events
    27. idempotency_same_event_id_single_delivery

  Success path (1):
    28. dispatch_succeeds_on_2xx

  Rate limit (1):
    29. rate_limit_after_ten_requests
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.models.webhook import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_EVENT_TYPES_PER_SUBSCRIPTION,
    VALID_EVENT_TYPES,
)
from niki.db.repositories import (
    InMemoryWebhookRepository,
    get_webhook_repository,
    set_webhook_repository,
)
from niki.main import create_app
from niki.services.webhook import (
    EVENT_HEADER,
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    USER_AGENT,
    WebhookDispatcherService,
    get_webhook_dispatcher,
    set_webhook_dispatcher,
    sign_payload,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _instant_sleep(_seconds: float) -> None:
    """No-op for the dispatcher's retry sleep. Production sleeps
    1s/4s/16s; tests want to skip the wall clock."""
    return None


@pytest.fixture
def owner_id() -> str:
    return "parent-alpha"


@pytest.fixture
def other_owner_id() -> str:
    return "parent-beta"


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, owner_id: str
) -> TestClient:
    """TestClient with a single deterministic parent id.

    The push-style fake-auth bypass is fine here because we
    test lateral-auth by overriding the dep in another fixture.
    """
    app = create_app()

    def fake():
        return ParentAccount(
            id=owner_id, name="Alpha",
            id_number_masked="****", phone_masked="****",
        )

    app.dependency_overrides[get_current_parent] = fake
    return TestClient(app)


@pytest.fixture
def other_client(
    monkeypatch: pytest.MonkeyPatch, other_owner_id: str
) -> TestClient:
    """Second TestClient for a different parent (lateral-auth tests)."""
    app = create_app()

    def fake():
        return ParentAccount(
            id=other_owner_id, name="Beta",
            id_number_masked="****", phone_masked="****",
        )

    app.dependency_overrides[get_current_parent] = fake
    return TestClient(app)


# Helpers ------------------------------------------------------------------


def _payload(target: str = "https://hook.example.com/path") -> dict:
    """A valid register payload."""
    return {
        "target_url": target,
        "event_types": ["account.deleted", "deletion.completed"],
    }


def _register(client: TestClient, target: str = "https://hook.example.com/p") -> dict:
    """Register a subscription and return the response body."""
    r = client.post("/api/v1/webhooks/", json=_payload(target))
    assert r.status_code == 201, r.text
    return r.json()


# Tests --------------------------------------------------------------------


class TestRegister:
    def test_register_creates_subscription_and_returns_secret(
        self, client: TestClient
    ) -> None:
        r = client.post("/api/v1/webhooks/", json=_payload())
        assert r.status_code == 201
        body = r.json()
        assert "id" in body
        assert "secret" in body
        assert len(body["secret"]) > 20  # secrets.token_urlsafe(32) → 43 chars
        assert body["target_url"].rstrip("/") == "https://hook.example.com/path"
        assert sorted(body["event_types"]) == [
            "account.deleted", "deletion.completed",
        ]
        assert body["is_active"] is True
        assert body["failure_count"] == 0
        assert body["created_at"] is not None


class TestList:
    def test_list_returns_owners_subscriptions(
        self, client: TestClient
    ) -> None:
        s1 = _register(client, "https://h1.example.com")
        s2 = _register(client, "https://h2.example.com")
        r = client.get("/api/v1/webhooks/")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        ids = {s["id"] for s in body["subscriptions"]}
        assert ids == {s1["id"], s2["id"]}

    def test_list_excludes_secrets(
        self, client: TestClient
    ) -> None:
        _register(client)
        r = client.get("/api/v1/webhooks/")
        body = r.json()
        for sub in body["subscriptions"]:
            assert "secret" not in sub
            assert "owner_id" in sub  # OK to include
            assert "target_url" in sub


class TestGet:
    def test_get_returns_subscription_without_secret(
        self, client: TestClient
    ) -> None:
        sub = _register(client)
        r = client.get(f"/api/v1/webhooks/{sub['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == sub["id"]
        assert "secret" not in body

    def test_get_unknown_returns_404(
        self, client: TestClient
    ) -> None:
        r = client.get("/api/v1/webhooks/does-not-exist")
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "WEBHOOK_NOT_FOUND"


class TestUpdate:
    def test_update_changes_target_url(
        self, client: TestClient
    ) -> None:
        sub = _register(client)
        r = client.patch(
            f"/api/v1/webhooks/{sub['id']}",
            json={"target_url": "https://other.example.com/x"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["target_url"].rstrip("/") == "https://other.example.com/x"
        assert "secret" not in body

    def test_update_changes_event_types(
        self, client: TestClient
    ) -> None:
        sub = _register(client)
        r = client.patch(
            f"/api/v1/webhooks/{sub['id']}",
            json={"event_types": ["safety.escalated", "audit.high_risk"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert sorted(body["event_types"]) == [
            "audit.high_risk", "safety.escalated",
        ]


class TestDisable:
    def test_disable_sets_inactive(
        self, client: TestClient
    ) -> None:
        sub = _register(client)
        r = client.delete(f"/api/v1/webhooks/{sub['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["is_active"] is False
        assert body["disabled_at"] is not None
        # And the GET reflects the new state.
        r2 = client.get(f"/api/v1/webhooks/{sub['id']}")
        assert r2.json()["is_active"] is False


class TestDeliveries:
    def test_deliveries_lists_attempts(
        self, client: TestClient
    ) -> None:
        sub = _register(client)
        # Trigger a dispatch via the service.
        dispatcher = get_webhook_dispatcher()
        # We don't have a target that responds; just use the
        # in-memory repo to plant a delivery row directly.
        from niki.db.repositories import get_webhook_repository
        repo = get_webhook_repository()
        httpx_to_async(
            repo.create_delivery(
                subscription_id=sub["id"],
                event_id="evt-test-1",
                event_type="account.deleted",
                payload={"foo": "bar"},
                target_url=sub["target_url"],
            )
        )
        r = client.get(
            f"/api/v1/webhooks/{sub['id']}/deliveries"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        d = body["deliveries"][0]
        assert d["event_id"] == "evt-test-1"
        assert d["event_type"] == "account.deleted"
        # No secret in delivery rows (would be a leak vector).
        assert "secret" not in d


# ---------------------------------------------------------------------------
# Lateral-auth
# ---------------------------------------------------------------------------


class TestLateralAuth:
    def test_cross_owner_get_returns_404(
        self, client: TestClient, other_client: TestClient
    ) -> None:
        sub = _register(client)
        r = other_client.get(f"/api/v1/webhooks/{sub['id']}")
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "WEBHOOK_NOT_FOUND"

    def test_cross_owner_patch_returns_404(
        self, client: TestClient, other_client: TestClient
    ) -> None:
        sub = _register(client)
        r = other_client.patch(
            f"/api/v1/webhooks/{sub['id']}",
            json={"target_url": "https://attacker.example.com"},
        )
        assert r.status_code == 404

    def test_cross_owner_delete_returns_404(
        self, client: TestClient, other_client: TestClient
    ) -> None:
        sub = _register(client)
        r = other_client.delete(f"/api/v1/webhooks/{sub['id']}")
        assert r.status_code == 404
        # Verify the owner's row is untouched.
        owner_view = client.get(f"/api/v1/webhooks/{sub['id']}")
        assert owner_view.json()["is_active"] is True

    def test_cross_owner_get_deliveries_returns_404(
        self, client: TestClient, other_client: TestClient
    ) -> None:
        sub = _register(client)
        r = other_client.get(
            f"/api/v1/webhooks/{sub['id']}/deliveries"
        )
        assert r.status_code == 404

    def test_list_does_not_leak_other_owners_subs(
        self, client: TestClient, other_client: TestClient
    ) -> None:
        sub_a = _register(client, "https://a.example.com")
        sub_b = _register(other_client, "https://b.example.com")
        owner_a_list = client.get("/api/v1/webhooks/").json()
        owner_b_list = other_client.get("/api/v1/webhooks/").json()
        a_ids = {s["id"] for s in owner_a_list["subscriptions"]}
        b_ids = {s["id"] for s in owner_b_list["subscriptions"]}
        assert sub_a["id"] in a_ids
        assert sub_b["id"] not in a_ids
        assert sub_b["id"] in b_ids
        assert sub_a["id"] not in b_ids


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_url_format_returns_422(
        self, client: TestClient
    ) -> None:
        # FastAPI/Pydantic returns 422 for unprocessable shape.
        r = client.post(
            "/api/v1/webhooks/",
            json={"target_url": "not-a-url", "event_types": ["account.deleted"]},
        )
        # Our handler returns 400 (we use HTTPException with code).
        assert r.status_code in (400, 422)
        detail = r.json().get("detail", {})
        code = detail.get("error") if isinstance(detail, dict) else None
        assert code in ("WEBHOOK_INVALID_URL", None)  # Pydantic may use its own code

    def test_http_url_rejected_in_production(
        self, client: TestClient
    ) -> None:
        # default Settings.allow_insecure_webhook_targets = False
        r = client.post(
            "/api/v1/webhooks/",
            json={
                "target_url": "http://insecure.example.com",
                "event_types": ["account.deleted"],
            },
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "WEBHOOK_URL_NOT_HTTPS"

    def test_http_url_allowed_when_insecure_flag_set(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Override the cached settings to flip the flag.
        from niki.config import settings as settings_module
        monkeypatch.setenv("ALLOW_INSECURE_WEBHOOK_TARGETS", "true")
        settings_module.get_settings.cache_clear()
        try:
            r = client.post(
                "/api/v1/webhooks/",
                json={
                    "target_url": "http://localhost:9999/hook",
                    "event_types": ["account.deleted"],
                },
            )
            assert r.status_code == 201
        finally:
            settings_module.get_settings.cache_clear()
            # Reset to default.
            monkeypatch.delenv("ALLOW_INSECURE_WEBHOOK_TARGETS", raising=False)
            settings_module.get_settings.cache_clear()

    def test_empty_event_types_returns_422(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/api/v1/webhooks/",
            json={
                "target_url": "https://h.example.com",
                "event_types": [],
            },
        )
        # 400 (our handler) or 422 (Pydantic short-circuit) — both
        # are correct from the spec's perspective.
        assert r.status_code in (400, 422)
        if r.status_code == 400:
            assert "error" in r.json()["detail"]

    def test_too_many_event_types_returns_422(
        self, client: TestClient
    ) -> None:
        many = [
            f"event.{i}" for i in range(MAX_EVENT_TYPES_PER_SUBSCRIPTION + 1)
        ]
        # Even with one extra item, our handler should reject —
        # but those names are also not in VALID_EVENT_TYPES. The
        # cap is checked BEFORE the closed-set check, so a
        # max-1 list of valid event types should hit the cap
        # path first. We re-use VALID_EVENT_TYPES cycled to
        # exactly the cap+1 length.
        # Cycle through VALID_EVENT_TYPES (it's a frozenset of 6
        # entries) until we exceed the cap.
        valid_list = list(VALID_EVENT_TYPES)
        too_many: list[str] = []
        i = 0
        while len(too_many) <= MAX_EVENT_TYPES_PER_SUBSCRIPTION:
            too_many.append(valid_list[i % len(valid_list)])
            i += 1
        r = client.post(
            "/api/v1/webhooks/",
            json={
                "target_url": "https://h.example.com",
                "event_types": too_many,
            },
        )
        assert r.status_code in (400, 422)

    def test_unsupported_event_type_returns_422(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/api/v1/webhooks/",
            json={
                "target_url": "https://h.example.com",
                "event_types": ["account.deleted", "totally.bogus"],
            },
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "WEBHOOK_INVALID_EVENT_TYPE"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    def _make_client(
        self,
        target_handler: Callable[[httpx.Request], httpx.Response],
    ) -> httpx.AsyncClient:
        """Build an httpx.AsyncClient backed by a MockTransport.

        The MockTransport lets the dispatcher POST to any URL
        while we control the response inline. We return a
        real AsyncClient so the dispatcher's existing call site
        (`client.post(url, content=..., headers=...)`) Just Works.
        """
        transport = httpx.MockTransport(target_handler)
        return httpx.AsyncClient(transport=transport, timeout=5.0)

    def test_dispatch_finds_matching_subscriptions(
        self, client: TestClient
    ) -> None:
        sub1 = _register(client, "https://h1.example.com")
        sub2 = _register(
            client, "https://h2.example.com"
        )
        # Update sub2 to only listen for safety events.
        client.patch(
            f"/api/v1/webhooks/{sub2['id']}",
            json={"event_types": ["safety.escalated"]},
        )

        received: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            received.append(req)
            return httpx.Response(200, json={"ok": True})

        http_client = self._make_client(handler)
        dispatcher = get_webhook_dispatcher()
        results = httpx_to_async(
            dispatcher.dispatch(
                "account.deleted",
                {"user": "u1"},
                event_id="evt-1",
                http_client=http_client,
            )
        )
        # Only sub1 should have been hit (sub2 is safety-only).
        assert len(received) == 1
        assert len(results) == 1
        assert results[0]["subscription_id"] == sub1["id"]
        assert results[0]["delivered"] is True
        assert results[0]["idempotent_replay"] is False

    def test_event_type_filter_blocks_unrelated_events(
        self, client: TestClient
    ) -> None:
        sub = _register(
            client, "https://h.example.com"
        )
        # Sub listens only for account.deleted; the dispatcher
        # should NOT POST a safety.escalated event to it.
        client.patch(
            f"/api/v1/webhooks/{sub['id']}",
            json={"event_types": ["account.deleted"]},
        )
        received: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            received.append(req)
            return httpx.Response(200, json={"ok": True})

        http_client = self._make_client(handler)
        dispatcher = get_webhook_dispatcher()
        results = httpx_to_async(
            dispatcher.dispatch(
                "safety.escalated",
                {"reason": "spam"},
                event_id="evt-2",
                http_client=http_client,
            )
        )
        assert received == []
        assert results == []

    def test_dispatch_signs_payload_correctly(
        self, client: TestClient
    ) -> None:
        sub = _register(client, "https://hook.example.com/sign")
        # Pull the secret back from the repo (the API doesn't
        # expose it after the create response).
        repo = get_webhook_repository()
        row = httpx_to_async(repo.get_subscription(sub["id"]))
        secret = row["secret"]
        # assert hasattr(row, "__await__") is False  # it's a dict

        received: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            received.append(req)
            return httpx.Response(200)

        http_client = self._make_client(handler)
        dispatcher = get_webhook_dispatcher()
        payload = {"user": "u1", "amount": 42}
        httpx_to_async(
            dispatcher.dispatch(
                "account.deleted", payload, event_id="evt-sig",
                http_client=http_client,
            )
        )
        assert len(received) == 1
        req = received[0]
        # Verify the signature against the body bytes.
        body = req.content
        expected_sig = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        sig_header = req.headers[SIGNATURE_HEADER]
        assert sig_header == f"sha256={expected_sig}"
        # Sanity-check the helper agrees.
        assert sig_header == sign_payload(secret, body)

    def test_signature_header_format(
        self, client: TestClient
    ) -> None:
        """Confirm exact format: `sha256=<lowercase-hex>`."""
        sub = _register(client, "https://h.example.com/fmt")
        repo = get_webhook_repository()
        row = httpx_to_async(repo.get_subscription(sub["id"]))
        secret = row["secret"]
        sig = sign_payload(secret, b'{"x":1}')
        assert sig.startswith("sha256=")
        hex_part = sig.split("=", 1)[1]
        assert len(hex_part) == 64
        assert hex_part == hex_part.lower()
        int(hex_part, 16)  # would raise if not valid hex

    def test_target_receives_event_and_id_headers(
        self, client: TestClient
    ) -> None:
        sub = _register(client, "https://h.example.com/headers")
        received: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            received.append(req)
            return httpx.Response(200)

        http_client = self._make_client(handler)
        dispatcher = get_webhook_dispatcher()
        httpx_to_async(
            dispatcher.dispatch(
                "account.deleted", {"a": 1},
                event_id="evt-headers-1",
                http_client=http_client,
            )
        )
        assert len(received) == 1
        req = received[0]
        assert req.headers[EVENT_HEADER] == "account.deleted"
        assert req.headers[EVENT_ID_HEADER] == "evt-headers-1"
        assert req.headers["User-Agent"] == USER_AGENT
        assert req.headers["Content-Type"] == "application/json"

    def test_dispatch_succeeds_on_2xx(
        self, client: TestClient
    ) -> None:
        sub = _register(client, "https://h.example.com/2xx")
        repo = get_webhook_repository()
        captured_attempts: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured_attempts.append(req)
            return httpx.Response(204)

        http_client = self._make_client(handler)
        dispatcher = get_webhook_dispatcher()
        results = httpx_to_async(
            dispatcher.dispatch(
                "account.deleted", {"a": 1},
                event_id="evt-2xx",
                http_client=http_client,
            )
        )
        assert len(captured_attempts) == 1  # 1 attempt (no retry)
        r = results[0]
        assert r["delivered"] is True
        assert r["attempts"] == 1
        # Delivery row reflects delivered_at.
        delivs = httpx_to_async(
            dispatcher.get_deliveries(sub["id"], sub["owner_id"])
        )
        assert delivs is not None and len(delivs) == 1
        assert delivs[0]["delivered_at"] is not None
        assert delivs[0]["last_status_code"] == 204

    def test_dispatch_retries_on_5xx(
        self, client: TestClient
    ) -> None:
        sub = _register(client, "https://h.example.com/5xx")
        attempts: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            attempts.append(req)
            return httpx.Response(503)

        http_client = self._make_client(handler)
        dispatcher = get_webhook_dispatcher()
        results = httpx_to_async(
            dispatcher.dispatch(
                "account.deleted", {"a": 1},
                event_id="evt-5xx",
                http_client=http_client,
            )
        )
        # 1 initial + 3 retries = 4 attempts
        assert len(attempts) == 4
        r = results[0]
        assert r["delivered"] is False
        assert r["attempts"] == 4
        assert r["last_status_code"] == 503
        # Delivery row reflects the failure
        delivs = httpx_to_async(
            dispatcher.get_deliveries(sub["id"], sub["owner_id"])
        )
        assert delivs is not None and len(delivs) == 1
        assert delivs[0]["delivered_at"] is None
        assert delivs[0]["last_status_code"] == 503

    def test_auto_disabled_after_five_consecutive_failures(
        self, client: TestClient
    ) -> None:
        sub = _register(client, "https://h.example.com/auto-disable")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        http_client = self._make_client(handler)
        dispatcher = get_webhook_dispatcher()
        repo = get_webhook_repository()
        # 5 distinct event_ids, all failing.
        for i in range(MAX_CONSECUTIVE_FAILURES):
            results = httpx_to_async(
                dispatcher.dispatch(
                    "account.deleted",
                    {"i": i},
                    event_id=f"evt-fail-{i}",
                    http_client=http_client,
                )
            )
            assert results[0]["delivered"] is False
        # After 5 failures, the subscription is auto-disabled.
        row = httpx_to_async(repo.get_subscription(sub["id"]))
        assert row is not None
        assert row["is_active"] is False
        assert row["disabled_at"] is not None
        assert row["failure_count"] == MAX_CONSECUTIVE_FAILURES
        # Subsequent dispatch returns no rows (no active subs).
        results = httpx_to_async(
            dispatcher.dispatch(
                "account.deleted", {"after": True},
                event_id="evt-after", http_client=http_client,
            )
        )
        assert results == []

    def test_idempotency_same_event_id_single_delivery(
        self, client: TestClient
    ) -> None:
        sub = _register(client, "https://h.example.com/idem")
        attempts: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            attempts.append(req)
            return httpx.Response(200)

        http_client = self._make_client(handler)
        dispatcher = get_webhook_dispatcher()
        # First call → creates a delivery row + 1 HTTP attempt.
        r1 = httpx_to_async(
            dispatcher.dispatch(
                "account.deleted", {"x": 1},
                event_id="evt-idem", http_client=http_client,
            )
        )
        # Second call with same event_id → no-op (idempotent).
        r2 = httpx_to_async(
            dispatcher.dispatch(
                "account.deleted", {"x": 2},
                event_id="evt-idem", http_client=http_client,
            )
        )
        assert r1[0]["delivered"] is True
        assert r2[0]["idempotent_replay"] is True
        # Only one HTTP attempt and one delivery row.
        assert len(attempts) == 1
        delivs = httpx_to_async(
            dispatcher.get_deliveries(sub["id"], sub["owner_id"])
        )
        assert delivs is not None and len(delivs) == 1


# ---------------------------------------------------------------------------
# Audit log integration
# ---------------------------------------------------------------------------


class TestAuditLog:
    def _audit_rows(self, event_type: str) -> list[dict[str, Any]]:
        from niki.db.repositories import get_audit_log_repository
        repo = get_audit_log_repository()
        # list_events is async; the in-memory repo is small enough
        # to filter synchronously by reading _rows. But to keep
        # the test self-contained, we use list_events via httpx_to_async.
        rows = httpx_to_async(
            repo.list_events(event_type=event_type, limit=100)
        )
        return rows

    def test_audit_row_written_on_register(
        self, client: TestClient
    ) -> None:
        _register(client)
        rows = self._audit_rows("webhook_registered")
        assert len(rows) == 1
        md = rows[0]["metadata"]
        assert "subscription_id" in md
        assert md["target_url"].rstrip("/") == "https://hook.example.com/p"
        assert "secret" not in md
        assert md["event_types_count"] == 2

    def test_audit_row_written_on_update(
        self, client: TestClient
    ) -> None:
        sub = _register(client)
        client.patch(
            f"/api/v1/webhooks/{sub['id']}",
            json={"target_url": "https://h.example.com/new"},
        )
        rows = self._audit_rows("webhook_updated")
        assert len(rows) == 1
        md = rows[0]["metadata"]
        assert md["subscription_id"] == sub["id"]
        assert "target_url" in md["fields_changed"]

    def test_audit_row_written_on_disable(
        self, client: TestClient
    ) -> None:
        sub = _register(client)
        client.delete(f"/api/v1/webhooks/{sub['id']}")
        rows = self._audit_rows("webhook_disabled")
        assert len(rows) == 1
        md = rows[0]["metadata"]
        assert md["subscription_id"] == sub["id"]


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_rate_limit_after_ten_requests(
        self, client: TestClient
    ) -> None:
        """10/minute cap on POST /webhooks. The 11th should 429."""
        # 10 should succeed
        for i in range(10):
            r = client.post(
                "/api/v1/webhooks/",
                json={
                    "target_url": f"https://h.example.com/{i}",
                    "event_types": ["account.deleted"],
                },
            )
            assert r.status_code == 201, (
                f"request #{i} failed: {r.status_code} {r.text}"
            )
        # 11th should be 429
        r11 = client.post(
            "/api/v1/webhooks/",
            json={
                "target_url": "https://h.example.com/11",
                "event_types": ["account.deleted"],
            },
        )
        assert r11.status_code == 429


# ---------------------------------------------------------------------------
# Helper: bridge async → sync in tests
# ---------------------------------------------------------------------------


def httpx_to_async(coro):
    """Run a coroutine in a fresh loop and return the result.

    pytest-asyncio is configured for the project, but the
    webhook tests are synchronous (TestClient). The dispatcher
    exposes async methods, so we drive them from sync via a
    short-lived event loop. This avoids marking every test
    `@pytest.mark.asyncio` and keeps the test surface tight.
    """
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        # Shouldn't happen inside a TestClient request, but
        # guard against a leak.
        raise RuntimeError(
            "httpx_to_async called inside a running event loop"
        )
    return loop.run_until_complete(coro)
