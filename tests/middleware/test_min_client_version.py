"""Tests for the min-client-version middleware (BE-COMP-003)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from niki.middleware.min_client_version import (
    MinClientVersionMiddleware,
    _parse_semver,
)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestParseSemver:
    def test_valid(self) -> None:
        assert _parse_semver("1.0.0") == (1, 0, 0)
        assert _parse_semver("1.2.3") == (1, 2, 3)
        assert _parse_semver("10.20.30") == (10, 20, 30)

    def test_invalid(self) -> None:
        assert _parse_semver("") is None
        assert _parse_semver("1.0") is None
        assert _parse_semver("1.0.0.0") is None
        assert _parse_semver("garbage") is None
        assert _parse_semver("v1.0.0") is None  # requires no prefix

    def test_ordering(self) -> None:
        # Correct semver ordering (lexicographic on tuple fails
        # for "1.10.0" vs "1.2.3").
        assert _parse_semver("1.2.3") < _parse_semver("1.10.0")
        assert _parse_semver("1.0.0") < _parse_semver("2.0.0")


# ---------------------------------------------------------------------------
# Middleware integration
# ---------------------------------------------------------------------------


def _make_app(min_version: str = "2.0.0") -> TestClient:
    app = FastAPI()

    @app.get("/probe")
    def probe():
        return {"ok": True}

    @app.get("/config/client")
    def config():
        return {"min_supported_version": min_version}

    app.add_middleware(
        MinClientVersionMiddleware,
        min_supported_version=min_version,
    )
    return TestClient(app)


class TestMinVersionMiddleware:
    def test_old_version_blocked(self) -> None:
        client = _make_app("2.0.0")
        r = client.get(
            "/probe",
            headers={"X-Client-Version": "1.5.0"},
        )
        assert r.status_code == 426
        body = r.json()
        assert body["error"] == "UPGRADE_REQUIRED"
        assert body["min_supported_version"] == "2.0.0"
        # Retry-After + Upgrade headers.
        assert r.headers.get("upgrade") == "2.0.0"
        assert r.headers.get("retry-after") == "86400"

    def test_exact_version_allowed(self) -> None:
        client = _make_app("2.0.0")
        r = client.get(
            "/probe",
            headers={"X-Client-Version": "2.0.0"},
        )
        assert r.status_code == 200

    def test_newer_version_allowed(self) -> None:
        client = _make_app("2.0.0")
        r = client.get(
            "/probe",
            headers={"X-Client-Version": "3.4.5"},
        )
        assert r.status_code == 200

    def test_missing_header_allowed(
        self,
    ) -> None:
        """Old clients may not send the header at all → allow."""
        client = _make_app("2.0.0")
        r = client.get("/probe")
        assert r.status_code == 200

    def test_garbage_header_allowed(self) -> None:
        client = _make_app("2.0.0")
        r = client.get(
            "/probe",
            headers={"X-Client-Version": "not-a-version"},
        )
        # Unparseable → let through (false-negative over
        # false-positive).
        assert r.status_code == 200

    def test_semver_ordering(self) -> None:
        """1.2.3 < 1.10.0 must block, not allow."""
        client = _make_app("1.10.0")
        r = client.get(
            "/probe",
            headers={"X-Client-Version": "1.2.3"},
        )
        assert r.status_code == 426
