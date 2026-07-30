"""Tests for the public status endpoint (BE-MISC-003)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from niki.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


class TestStatusEndpoint:
    def test_returns_200_with_structure(
        self, client: TestClient
    ) -> None:
        """The endpoint is public + always 200 (degraded deps
        don't escalate the HTTP status — that's the operator's
        call to make from the body)."""
        r = client.get("/api/v1/status")
        assert r.status_code == 200
        body = r.json()
        assert "status" in body
        assert body["status"] in ("healthy", "degraded")
        assert "components" in body
        assert "version" in body
        assert "checked_at" in body

    def test_lists_known_components(
        self, client: TestClient
    ) -> None:
        r = client.get("/api/v1/status")
        names = [c["name"] for c in r.json()["components"]]
        for expected in ("redis", "postgres", "chroma", "neo4j"):
            assert expected in names

    def test_no_required_auth(self, client: TestClient) -> None:
        """Status is public — no token required."""
        r = client.get("/api/v1/status")
        assert r.status_code == 200

    def test_redis_failure_degrades(
        self, client: TestClient
    ) -> None:
        """If Redis is unhealthy, the overall status is degraded
        but the endpoint still returns 200."""
        with patch(
            "niki.api.status.RedisClient.health_check",
            return_value={
                "name": "redis", "status": "unhealthy",
                "error": "ConnectionError",
            },
        ):
            r = client.get("/api/v1/status")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "degraded"
        # Find the redis entry and check it's marked unhealthy.
        redis = next(
            c for c in body["components"] if c["name"] == "redis"
        )
        assert redis["status"] == "unhealthy"

    def test_check_exception_does_not_crash(
        self, client: TestClient
    ) -> None:
        """A raised exception in a check function must NOT 500
        the endpoint; it's reported as unhealthy instead."""
        with patch(
            "niki.api.status.RedisClient.health_check",
            side_effect=ConnectionError("redis is down"),
        ):
            r = client.get("/api/v1/status")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "degraded"
