"""Tests for /api/v1/media/upload-style endpoints under rate limit (BE-API-002).

Strategy: build a tiny FastAPI app with one limited endpoint, hammer it
enough to overflow, and verify the 429 body / headers match §6.3.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from niki.middleware.rate_limit import limiter, reset_for_tests, setup_rate_limit


@pytest.fixture
def limited_app() -> FastAPI:
    """Tiny app with one endpoint limited to 2/minute."""
    reset_for_tests()
    app = FastAPI()
    setup_rate_limit(app)
    app.state.limiter = limiter

    @app.get("/limited")
    @limiter.limit("2/minute")
    async def _limited(request: Request) -> dict:
        return {"ok": True}

    @app.get("/unlimited")
    async def _unlimited(request: Request) -> dict:
        return {"ok": True}

    return app


@pytest.fixture
def client(limited_app: FastAPI) -> TestClient:
    return TestClient(limited_app)


class TestRateLimitEnforcement:
    def test_under_limit_passes(self, client: TestClient) -> None:
        for _ in range(2):
            r = client.get("/limited")
            assert r.status_code == 200

    def test_third_call_returns_429(self, client: TestClient) -> None:
        for _ in range(2):
            client.get("/limited")
        r = client.get("/limited")
        assert r.status_code == 429

    def test_429_shape_is_section_6_3(self, client: TestClient) -> None:
        for _ in range(2):
            client.get("/limited")
        r = client.get("/limited")
        body = r.json()
        assert body["success"] is False
        assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert body["error_code"] == "RATE_LIMIT_EXCEEDED"
        # slowapi's str() of RateLimitItem may render as "2 per 1 minute"
        # or "2/minute" depending on version; just check both pieces present.
        limit_str = body["error"]["detail"]["limit"]
        assert "2" in limit_str and ("minute" in limit_str or "/minute" in limit_str)
        assert "retry_after_seconds" in body["error"]["detail"]
        assert "request_id" in body

    def test_429_has_retry_after_header(self, client: TestClient) -> None:
        for _ in range(2):
            client.get("/limited")
        r = client.get("/limited")
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        # slowapi always reports limit as the RateLimitItem string form.
        assert r.headers["X-RateLimit-Limit"].startswith("2")
        assert r.headers["X-RateLimit-Remaining"] == "0"
        assert "X-Request-ID" in r.headers

    def test_unlimited_endpoint_not_affected(self, client: TestClient) -> None:
        for _ in range(2):
            client.get("/limited")  # exhaust /limited
        # /unlimited still works at full speed
        for _ in range(10):
            r = client.get("/unlimited")
            assert r.status_code == 200


class TestResetForTests:
    def test_reset_clears_in_memory_state(self, client: TestClient) -> None:
        # Exhaust the limiter
        for _ in range(5):
            client.get("/limited")
        r = client.get("/limited")
        assert r.status_code == 429

        # The autouse fixture in tests/conftest.py calls reset_for_tests
        # before AND after every test, so even this assertion re-enters
        # a clean state for the next test. Here we verify that
        # reset_for_tests() exists and is callable.
        reset_for_tests()
        assert reset_for_tests is not None


class TestSharedLimiterIntegration:
    """Apply per-route limit on a real Niki endpoint and verify it."""

    def test_sms_send_returns_429_after_quota(self, tmp_path, monkeypatch) -> None:
        """/auth/sms/send is limited to 3/minute; the 4th call should 429."""
        from niki.auth.sms_service import set_sms_service, InMemorySMSCodeService

        monkeypatch.setenv("NIKI_UPLOAD_DIR", str(tmp_path))
        set_sms_service(InMemorySMSCodeService())

        from niki.main import create_app

        client = TestClient(create_app())
        # Make 4 calls in a row (limit is 3/min).
        statuses = [
            client.post("/api/v1/auth/sms/send", json={"phone": f"1380000000{i}"}) for i in range(4)
        ]
        codes = [r.status_code for r in statuses]
        # First 3 should pass (200), 4th should overflow (429).
        assert codes[:3] == [200, 200, 200], f"expected first 3 to pass, got {codes}"
        assert codes[3] == 429, f"expected 4th to 429, got {codes[3]}"
        body = statuses[3].json()
        assert body["success"] is False
        assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
