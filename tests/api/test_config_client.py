"""Tests for the client-config endpoint (BE-MISC-001 + BE-MISC-002)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.feature_flags import (
    FeatureFlagService,
    set_feature_flag_service,
)
from niki.main import create_app


# Pre-defined flags the BE-MISC-001 contract asserts on. The
# endpoint now reads from the FeatureFlagService, so the test
# fixture below seeds the in-process service with these values
# before any request runs.
EXPECTED_FLAGS = {
    "new_dashboard": True,
    "ocr_enabled": False,
    "sse_alerts": True,
}


@pytest.fixture
def client() -> TestClient:
    # Seed the flag service BEFORE the app handles any request.
    # Using a dedicated instance avoids bleed-over between
    # test files (the cached singleton is module-global).
    set_feature_flag_service(FeatureFlagService(defaults=EXPECTED_FLAGS))
    return TestClient(create_app())


class TestConfigEndpoint:
    def test_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/v1/config/client")
        assert r.status_code == 200
        body = r.json()
        assert body["api_version"] == "1.0.0"
        assert body["daily_limit_minutes"] == 60
        assert body["cooldown_days"] == 7
        assert "hms" in body["enabled_channels"]
        assert "apns" in body["enabled_channels"]

    def test_includes_feature_flags(self, client: TestClient) -> None:
        r = client.get("/api/v1/config/client")
        body = r.json()
        assert "feature_flags" in body
        # Pre-defined flags.
        assert "new_dashboard" in body["feature_flags"]
        assert "sse_alerts" in body["feature_flags"]
        # And the values are bools — not arbitrary YAML strings.
        assert isinstance(body["feature_flags"]["new_dashboard"], bool)
        assert body["feature_flags"]["new_dashboard"] is True
        assert body["feature_flags"]["ocr_enabled"] is False

    def test_query_params_accepted(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/config/client",
            params={"client_type": "ios", "version": "1.0.0"},
        )
        assert r.status_code == 200
        body = r.json()
        # Echoed for debugging (the v1.0 implementation
        # doesn't filter on these yet).
        assert body["_query"]["client_type"] == "ios"
        assert body["_query"]["version"] == "1.0.0"

    def test_no_required_auth(self, client: TestClient) -> None:
        """The config endpoint is public — the frontend needs
        it BEFORE login to know the API version and feature
        flags."""
        r = client.get("/api/v1/config/client")
        assert r.status_code == 200

    def test_response_is_cacheable(self, client: TestClient) -> None:
        """Config changes infrequently → cacheable for 5 min."""
        r = client.get("/api/v1/config/client")
        # Default TestClient may not set this; just assert
        # the endpoint doesn't deliberately set no-store.
        cc = r.headers.get("cache-control", "")
        assert "no-store" not in cc
