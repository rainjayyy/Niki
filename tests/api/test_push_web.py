"""Tests for BE-PUSH-003 (Web Push delivery via VAPID).

Covers:
    WebPushDeliveryBackend.deliver        — status ladder + VAPID args
    CompositePushDeliveryService.deliver  — per-platform dispatch
    get_or_generate_vapid_keys()          — generation + idempotence
    GET /api/v1/push/vapid-public-key     — public key for the browser
    GET /static/service-worker.js         — service worker asset

`pywebpush` is an optional runtime dependency and is deliberately
NOT imported here: every test injects a fake `webpush_fn`, which is
the same seam the production backend resolves lazily. That keeps the
suite green on a box where the library isn't installed, and keeps it
from ever making a real network call if it is.
"""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from niki.config.settings import (
    Settings,
    get_or_generate_vapid_keys,
    get_settings,
)
from niki.db.repositories import (
    InMemoryAuditLogRepository,
    set_audit_log_repository,
)
from niki.i18n.messages import MESSAGES_EN, MESSAGES_ZH
from niki.main import STATIC_DIR, create_app
from niki.services.audit_log import (
    AuditLogService,
    set_audit_log_service,
)
from niki.services.push_delivery import (
    EVENT_WEB_PUSH_DELIVERED,
    EVENT_WEB_PUSH_DRY_RUN,
    EVENT_WEB_PUSH_FAILED,
    STATUS_DELIVERED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_SKIPPED_NO_TOKEN,
    CompositePushDeliveryService,
    PushDeliveryService,
    PushPayload,
    WebPushDeliveryBackend,
)

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abcdef-subscription-secret"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for the `requests.Response` pywebpush returns."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = ""


class _FakeWebPush:
    """Records the kwargs pywebpush would have been called with."""

    def __init__(self, *, status_code: int = 201, raises: BaseException | None = None):
        self.status_code = status_code
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return _FakeResponse(self.status_code)

    @property
    def last(self) -> dict[str, Any]:
        assert self.calls, "webpush was never called"
        return self.calls[-1]


def _device(
    *,
    device_id: str = "web-dev-1",
    platform: str = "web",
    push_token: str = ENDPOINT,
    channel: str = "web",
    parent_id: str = "test_parent",
) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "platform": platform,
        "push_token": push_token,
        "channel": channel,
        "parent_id": parent_id,
        "p256dh": "BFakeP256dhKeyValue",
        "auth": "FakeAuthSecret",
    }


def _payload(**kwargs: Any) -> PushPayload:
    base = {"title": "安全提醒", "body": "孩子触发了一条高危规则", "severity": "warning"}
    base.update(kwargs)
    return PushPayload(**base)  # type: ignore[arg-type]


def _settings_with_keys(tmp_path: Path) -> Settings:
    """A throwaway Settings whose VAPID cache lives under tmp_path."""
    return Settings(
        vapid_private_key_file=str(tmp_path / "priv.pem"),
        vapid_public_key_file=str(tmp_path / "pub.b64"),
    )


def _backend(
    tmp_path: Path,
    webpush_fn: Any,
    audit: AuditLogService | None = None,
) -> WebPushDeliveryBackend:
    return WebPushDeliveryBackend(
        webpush_fn=webpush_fn,
        settings=_settings_with_keys(tmp_path),
        audit_service=audit,
    )


@pytest.fixture
def audit_repo() -> InMemoryAuditLogRepository:
    """Fresh in-memory audit repo wired into the singleton service."""
    repo = InMemoryAuditLogRepository()
    set_audit_log_repository(repo)
    set_audit_log_service(AuditLogService(repo))
    yield repo
    set_audit_log_service(AuditLogService())


# ---------------------------------------------------------------------------
# WebPushDeliveryBackend — status ladder
# ---------------------------------------------------------------------------


class TestWebPushDeliveryStatuses:
    def test_delivered_on_201(self, tmp_path: Path) -> None:
        fake = _FakeWebPush(status_code=201)
        backend = _backend(tmp_path, fake)

        result = asyncio.run(backend.deliver(_device(), _payload()))

        assert result.status == STATUS_DELIVERED
        assert result.error is None
        assert result.dry_run is False
        assert result.latency_ms is not None and result.latency_ms >= 0
        assert len(fake.calls) == 1

    def test_failed_on_4xx(self, tmp_path: Path) -> None:
        fake = _FakeWebPush(status_code=410)  # Gone — subscription dead
        backend = _backend(tmp_path, fake)

        result = asyncio.run(backend.deliver(_device(), _payload()))

        assert result.status == STATUS_FAILED
        assert result.error == "HTTP_410"

    def test_failed_on_5xx(self, tmp_path: Path) -> None:
        fake = _FakeWebPush(status_code=503)
        backend = _backend(tmp_path, fake)

        result = asyncio.run(backend.deliver(_device(), _payload()))

        assert result.status == STATUS_FAILED
        assert result.error == "HTTP_503"

    def test_failed_on_network_error(self, tmp_path: Path) -> None:
        fake = _FakeWebPush(raises=ConnectionError("dns went sideways"))
        backend = _backend(tmp_path, fake)

        result = asyncio.run(backend.deliver(_device(), _payload()))

        assert result.status == STATUS_FAILED
        assert result.error is not None
        assert "NETWORK_ERROR" in result.error

    def test_skipped_for_non_web_platform(self, tmp_path: Path) -> None:
        fake = _FakeWebPush()
        backend = _backend(tmp_path, fake)

        result = asyncio.run(backend.deliver(_device(platform="android"), _payload()))

        assert result.status == STATUS_SKIPPED_NO_TOKEN
        assert fake.calls == [], "must not touch the network for a non-web device"

    def test_skipped_for_non_https_token(self, tmp_path: Path) -> None:
        fake = _FakeWebPush()
        backend = _backend(tmp_path, fake)

        result = asyncio.run(backend.deliver(_device(push_token="hms-vendor-token"), _payload()))

        assert result.status == STATUS_SKIPPED_NO_TOKEN
        assert fake.calls == []

    def test_dry_run_skips_the_network_call(self, tmp_path: Path) -> None:
        fake = _FakeWebPush()
        backend = _backend(tmp_path, fake)

        result = asyncio.run(backend.deliver(_device(), _payload(), dry_run=True))

        assert result.status == STATUS_DRY_RUN
        assert result.dry_run is True
        assert fake.calls == []
        assert result.latency_ms is not None
        assert 10 <= result.latency_ms <= 50


# ---------------------------------------------------------------------------
# WebPushDeliveryBackend — the arguments pywebpush actually receives
# ---------------------------------------------------------------------------


class TestWebPushCallArguments:
    def test_vapid_private_key_and_claims_are_passed(self, tmp_path: Path) -> None:
        settings = _settings_with_keys(tmp_path)
        fake = _FakeWebPush()
        backend = WebPushDeliveryBackend(webpush_fn=fake, settings=settings)

        asyncio.run(backend.deliver(_device(), _payload()))

        kwargs = fake.last
        assert "BEGIN PRIVATE KEY" in kwargs["vapid_private_key"]
        assert kwargs["vapid_claims"] == {"sub": settings.vapid_subject}

    def test_ttl_defaults_to_86400(self, tmp_path: Path) -> None:
        fake = _FakeWebPush()
        backend = _backend(tmp_path, fake)

        asyncio.run(backend.deliver(_device(), _payload()))

        assert fake.last["ttl"] == 86400
        assert fake.last["headers"]["TTL"] == "86400"

    def test_urgency_header_is_set(self, tmp_path: Path) -> None:
        fake = _FakeWebPush()
        backend = _backend(tmp_path, fake)

        asyncio.run(backend.deliver(_device(), _payload()))

        assert fake.last["headers"]["Urgency"] == "normal"

    def test_ttl_and_urgency_follow_settings(self, tmp_path: Path) -> None:
        settings = _settings_with_keys(tmp_path)
        settings.web_push_ttl_seconds = 60
        settings.web_push_urgency = "high"
        fake = _FakeWebPush()
        backend = WebPushDeliveryBackend(webpush_fn=fake, settings=settings)

        asyncio.run(backend.deliver(_device(), _payload()))

        assert fake.last["ttl"] == 60
        assert fake.last["headers"]["Urgency"] == "high"

    def test_dedupe_key_becomes_topic_header(self, tmp_path: Path) -> None:
        fake = _FakeWebPush()
        backend = _backend(tmp_path, fake)

        asyncio.run(backend.deliver(_device(), _payload(dedupe_key="alert-42")))

        assert fake.last["headers"]["Topic"] == "alert-42"

    def test_endpoint_only_token_borrows_device_keys(self, tmp_path: Path) -> None:
        fake = _FakeWebPush()
        backend = _backend(tmp_path, fake)

        asyncio.run(backend.deliver(_device(), _payload()))

        sub = fake.last["subscription_info"]
        assert sub["endpoint"] == ENDPOINT
        assert sub["keys"] == {
            "p256dh": "BFakeP256dhKeyValue",
            "auth": "FakeAuthSecret",
        }

    def test_full_subscription_json_token_is_passed_through(self, tmp_path: Path) -> None:
        token = (
            '{"endpoint": "https://updates.push.services.mozilla.com/wpush/v2/x",'
            ' "keys": {"p256dh": "REAL_P256DH", "auth": "REAL_AUTH"}}'
        )
        fake = _FakeWebPush()
        backend = _backend(tmp_path, fake)

        asyncio.run(backend.deliver(_device(push_token=token), _payload()))

        sub = fake.last["subscription_info"]
        assert sub["endpoint"].startswith("https://updates.push.services.mozilla.com")
        assert sub["keys"]["p256dh"] == "REAL_P256DH"

    def test_payload_is_json_encoded(self, tmp_path: Path) -> None:
        fake = _FakeWebPush()
        backend = _backend(tmp_path, fake)

        asyncio.run(backend.deliver(_device(), _payload(title="标题")))

        assert '"title": "标题"' in fake.last["data"]


# ---------------------------------------------------------------------------
# VAPID key management
# ---------------------------------------------------------------------------


class TestVapidKeyGeneration:
    def test_generates_a_pair_when_nothing_is_configured(self, tmp_path: Path) -> None:
        settings = _settings_with_keys(tmp_path)

        private_pem, public_b64 = get_or_generate_vapid_keys(settings)

        assert "BEGIN PRIVATE KEY" in private_pem
        # base64url, unpadded, decoding to a 65-byte uncompressed point.
        assert "=" not in public_b64
        raw = base64.urlsafe_b64decode(public_b64 + "==")
        assert len(raw) == 65
        assert raw[0] == 0x04

    def test_second_call_returns_the_same_pair(self, tmp_path: Path) -> None:
        settings = _settings_with_keys(tmp_path)

        first = get_or_generate_vapid_keys(settings)
        second = get_or_generate_vapid_keys(settings)

        assert first == second

    def test_pair_survives_a_fresh_settings_object_via_disk_cache(self, tmp_path: Path) -> None:
        """A restart must not invalidate existing browser subscriptions."""
        first = get_or_generate_vapid_keys(_settings_with_keys(tmp_path))
        second = get_or_generate_vapid_keys(_settings_with_keys(tmp_path))

        assert first == second
        assert (tmp_path / "priv.pem").is_file()
        assert (tmp_path / "pub.b64").is_file()

    def test_different_cache_dirs_yield_different_pairs(self, tmp_path: Path) -> None:
        a = get_or_generate_vapid_keys(_settings_with_keys(tmp_path / "a"))
        b = get_or_generate_vapid_keys(_settings_with_keys(tmp_path / "b"))

        assert a != b

    def test_explicit_settings_win_over_generation(self, tmp_path: Path) -> None:
        settings = _settings_with_keys(tmp_path)
        settings.vapid_private_key = "-----BEGIN PRIVATE KEY-----\nCONFIGURED\n"
        settings.vapid_public_key = "CONFIGURED_PUBLIC"

        private_pem, public_b64 = get_or_generate_vapid_keys(settings)

        assert public_b64 == "CONFIGURED_PUBLIC"
        assert "CONFIGURED" in private_pem
        assert not (tmp_path / "priv.pem").exists()


# ---------------------------------------------------------------------------
# Composite dispatch
# ---------------------------------------------------------------------------


class _RecordingBackend(PushDeliveryService):
    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def deliver(self, device, payload, *, dry_run: bool = False):
        from niki.services.push_delivery import PushDeliveryResult

        self.seen.append(device)
        return PushDeliveryResult(
            device_id="web-dev-1",
            channel="web",
            status=STATUS_DELIVERED,
            latency_ms=7,
            dry_run=dry_run,
        )


class TestCompositeDispatch:
    def test_web_platform_routes_to_the_web_backend(self) -> None:
        recorder = _RecordingBackend()
        svc = CompositePushDeliveryService(web_backend=recorder)

        result = asyncio.run(svc.deliver(_device(), _payload()))

        assert result.status == STATUS_DELIVERED
        assert len(recorder.seen) == 1

    def test_android_platform_falls_back_to_the_dry_run_stub(self) -> None:
        recorder = _RecordingBackend()
        svc = CompositePushDeliveryService(web_backend=recorder)

        result = asyncio.run(
            svc.deliver(
                _device(platform="android", push_token="fcm-token"),
                _payload(),
                dry_run=True,
            )
        )

        assert result.status == STATUS_DRY_RUN
        assert recorder.seen == [], "android must not reach the web backend"

    def test_web_deliveries_still_land_in_the_history_ring_buffer(
        self,
    ) -> None:
        recorder = _RecordingBackend()
        svc = CompositePushDeliveryService(web_backend=recorder)

        asyncio.run(svc.deliver(_device(), _payload()))

        history = svc.history()
        assert len(history) == 1
        assert history[0].channel == "web"

    def test_default_web_backend_is_the_real_one(self) -> None:
        svc = CompositePushDeliveryService()
        assert isinstance(svc.web_backend, WebPushDeliveryBackend)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class TestWebPushAudit:
    def test_audit_row_on_successful_delivery(
        self, tmp_path: Path, audit_repo: InMemoryAuditLogRepository
    ) -> None:
        backend = _backend(tmp_path, _FakeWebPush(status_code=201))

        asyncio.run(backend.deliver(_device(), _payload()))

        rows = asyncio.run(audit_repo.list_events(limit=10))
        assert len(rows) == 1
        assert rows[0]["event_type"] == EVENT_WEB_PUSH_DELIVERED
        assert rows[0]["severity"] == "info"
        meta = rows[0]["metadata"]
        assert meta["subscription_id"] == "web-dev-1"
        assert meta["status_code"] == 201

    def test_audit_row_on_failure_is_a_warning(
        self, tmp_path: Path, audit_repo: InMemoryAuditLogRepository
    ) -> None:
        backend = _backend(tmp_path, _FakeWebPush(status_code=404))

        asyncio.run(backend.deliver(_device(), _payload()))

        rows = asyncio.run(audit_repo.list_events(limit=10))
        assert len(rows) == 1
        assert rows[0]["event_type"] == EVENT_WEB_PUSH_FAILED
        assert rows[0]["severity"] == "warning"
        assert rows[0]["metadata"]["status_code"] == 404

    def test_audit_row_on_dry_run(
        self, tmp_path: Path, audit_repo: InMemoryAuditLogRepository
    ) -> None:
        backend = _backend(tmp_path, _FakeWebPush())

        asyncio.run(backend.deliver(_device(), _payload(), dry_run=True))

        rows = asyncio.run(audit_repo.list_events(limit=10))
        assert len(rows) == 1
        assert rows[0]["event_type"] == EVENT_WEB_PUSH_DRY_RUN

    def test_audit_never_records_the_subscription_path(
        self, tmp_path: Path, audit_repo: InMemoryAuditLogRepository
    ) -> None:
        """The endpoint path IS the push credential — host only."""
        backend = _backend(tmp_path, _FakeWebPush(status_code=201))

        asyncio.run(backend.deliver(_device(), _payload()))

        meta = asyncio.run(audit_repo.list_events(limit=1))[0]["metadata"]
        assert meta["endpoint"] == "fcm.googleapis.com"
        assert "subscription-secret" not in str(meta)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


class TestVapidPublicKeyEndpoint:
    def test_returns_the_servers_public_key(self, client: TestClient) -> None:
        response = client.get("/api/v1/push/vapid-public-key")

        assert response.status_code == 200
        body = response.json()
        assert body["public_key"]
        _private, public = get_or_generate_vapid_keys()
        assert body["public_key"] == public
        assert body["subject"] == get_settings().vapid_subject

    def test_requires_no_auth(self, client: TestClient) -> None:
        """No Authorization header at all — still 200."""
        response = client.get(
            "/api/v1/push/vapid-public-key",
            headers={"Accept": "application/json"},
        )

        assert response.status_code == 200
        assert "public_key" in response.json()

    def test_key_is_stable_across_calls(self, client: TestClient) -> None:
        first = client.get("/api/v1/push/vapid-public-key").json()
        second = client.get("/api/v1/push/vapid-public-key").json()

        assert first["public_key"] == second["public_key"]


class TestStaticAssets:
    def test_service_worker_is_served_as_javascript(self, client: TestClient) -> None:
        response = client.get("/static/service-worker.js")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/javascript")
        assert "addEventListener('push'" in response.text
        assert "addEventListener('notificationclick'" in response.text

    def test_service_worker_may_claim_the_root_scope(self, client: TestClient) -> None:
        response = client.get("/static/service-worker.js")

        assert response.headers.get("Service-Worker-Allowed") == "/"

    def test_push_helper_is_served(self, client: TestClient) -> None:
        response = client.get("/static/push-helper.js")

        assert response.status_code == 200
        assert "urlB64ToUint8Array" in response.text
        assert "subscribeToPush" in response.text

    def test_service_worker_handles_subscription_change(self) -> None:
        source = (STATIC_DIR / "service-worker.js").read_text(encoding="utf-8")
        assert "pushsubscriptionchange" in source
        assert "urlB64ToUint8Array" in source


class TestUrlB64ToUint8Array:
    """The JS helper's contract, pinned from the Python side.

    We can't run JS here, so we assert two things: the helper applies
    the two transformations base64url needs (re-pad, alphabet swap),
    and the key we hand it actually decodes to a 65-byte P-256 point
    under exactly those rules.
    """

    def test_helper_repads_and_swaps_the_alphabet(self) -> None:
        source = (STATIC_DIR / "push-helper.js").read_text(encoding="utf-8")
        body = source[source.index("export function urlB64ToUint8Array") :]
        body = body[: body.index("\n}")]

        assert re.search(r"'='\.repeat\(\(4 - \(base64String\.length % 4\)\) % 4\)", body)
        assert ".replace(/-/g, '+')" in body
        assert ".replace(/_/g, '/')" in body
        assert "Uint8Array" in body

    def test_server_key_decodes_under_those_rules(self) -> None:
        _private, public_b64 = get_or_generate_vapid_keys()

        # Exactly what the JS does: re-pad, swap alphabet, decode.
        padded = public_b64 + "=" * ((4 - len(public_b64) % 4) % 4)
        raw = base64.b64decode(padded.replace("-", "+").replace("_", "/"))

        assert len(raw) == 65
        assert raw[0] == 0x04


class TestWebPushI18nKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "push.web_unsupported_browser",
            "push.web_subscription_failed",
            "push.web_vapid_key_unavailable",
            "push.web_unsubscribe_failed",
        ],
    )
    def test_key_exists_in_both_catalogs(self, key: str) -> None:
        assert key in MESSAGES_ZH and MESSAGES_ZH[key]
        assert key in MESSAGES_EN and MESSAGES_EN[key]
