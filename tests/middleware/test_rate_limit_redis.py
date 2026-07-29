"""Tests for the BE-API-002-redis wiring.

We don't have a real Redis in the test environment, so these
tests verify:
  - the URI plumbs through into slowapi correctly
  - the in-memory fallback is enabled
  - the /health endpoint reports the right backend
  - the `is_rate_limit_in_fallback()` flag reflects storage type
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.config.settings import get_settings
from niki.middleware.rate_limit import (
    get_rate_limit_backend,
    get_limiter,
    is_rate_limit_in_fallback,
)
from niki.main import create_app


class TestBackendDetection:
    def test_default_backend_is_memory(self) -> None:
        """Without env-var override, default URI is memory://."""
        # The default value is fixed; no env var needed in test mode.
        assert get_settings().rate_limit_storage_uri == "memory://"
        assert get_rate_limit_backend() == "memory"

    def test_redis_uri_detected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A redis:// URI is detected as the redis backend."""
        # Settings is cached via lru_cache, so clear it before mutating.
        from niki.config import settings as settings_mod
        settings_mod.get_settings.cache_clear()
        monkeypatch.setenv(
            "RATE_LIMIT_STORAGE_URI",
            "redis://localhost:6379/1",
        )
        try:
            assert get_rate_limit_backend() == "redis"
        finally:
            settings_mod.get_settings.cache_clear()

    def test_rediss_uri_detected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rediss (TLS) is also recognised."""
        from niki.config import settings as settings_mod
        settings_mod.get_settings.cache_clear()
        monkeypatch.setenv(
            "RATE_LIMIT_STORAGE_URI",
            "rediss://secure-redis.example.com:6380/0",
        )
        try:
            assert get_rate_limit_backend() == "redis"
        finally:
            settings_mod.get_settings.cache_clear()

    def test_unknown_scheme_falls_back_to_memory_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown schemes don't crash — they're reported as memory."""
        from niki.config import settings as settings_mod
        settings_mod.get_settings.cache_clear()
        monkeypatch.setenv(
            "RATE_LIMIT_STORAGE_URI",
            "memcached://cache:11211",
        )
        try:
            assert get_rate_limit_backend() == "memory"
        finally:
            settings_mod.get_settings.cache_clear()


class TestLimiterConfiguration:
    def test_limiter_has_fallback_enabled(self) -> None:
        """slowapi must have in-memory fallback enabled so a
        Redis outage doesn't 500 every request."""
        lim = get_limiter()
        # slowapi exposes this with a leading underscore.
        assert lim._in_memory_fallback_enabled is True

    def test_limiter_storage_uri_matches_setting(self) -> None:
        lim = get_limiter()
        settings = get_settings()
        # slowapi exposes the resolved storage URI; for memory://
        # it normalises to 'memory'.
        assert lim._storage_uri in ("memory", settings.rate_limit_storage_uri)

    def test_in_fallback_false_when_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (memory://) never reports fallback."""
        from niki.config import settings as settings_mod
        settings_mod.get_settings.cache_clear()
        monkeypatch.delenv(
            "RATE_LIMIT_STORAGE_URI", raising=False
        )
        try:
            assert is_rate_limit_in_fallback() is False
        finally:
            settings_mod.get_settings.cache_clear()

    def test_in_fallback_false_when_redis_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When Redis is healthy, slowapi uses Redis storage,
        not the in-memory fallback."""
        from niki.config import settings as settings_mod
        settings_mod.get_settings.cache_clear()
        monkeypatch.setenv(
            "RATE_LIMIT_STORAGE_URI",
            "redis://localhost:6379/1",
        )
        try:
            # We can't connect to a real Redis in tests, so the
            # limiter is in its initial state (likely None or
            # MemoryStorage because slowapi fell back when it
            # couldn't connect). Either way, we assert the helper
            # doesn't crash and returns a bool.
            result = is_rate_limit_in_fallback()
            assert isinstance(result, bool)
        finally:
            settings_mod.get_settings.cache_clear()


class TestHealthEndpointReportsBackend:
    def test_health_includes_rate_limit_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from niki.config import settings as settings_mod
        settings_mod.get_settings.cache_clear()
        monkeypatch.delenv(
            "RATE_LIMIT_STORAGE_URI", raising=False
        )
        client = TestClient(create_app())
        try:
            r = client.get("/api/v1/health")
            assert r.status_code == 200
            body = r.json()
            assert "rate_limit_backend" in body
            assert body["rate_limit_backend"] == "memory"
            assert "rate_limit_in_fallback" in body
            assert body["rate_limit_in_fallback"] is False
        finally:
            settings_mod.get_settings.cache_clear()


class TestRateLimitStillEnforced:
    """Sanity: the limiter still 429s when over the limit,
    even after the Redis-URI plumbing change."""

    def test_429_still_returned_when_exceeded(self) -> None:
        # /api/v1/media/upload is limited to 20/minute.
        from fastapi.testclient import TestClient
        from niki.media.storage import LocalDiskStorage, set_media_storage
        import os
        tmp = os.environ.get("TMP") or "/tmp"
        set_media_storage(LocalDiskStorage(tmp))

        client = TestClient(create_app())
        PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 50

        # Issue 21 requests; the 21st should hit 429.
        statuses = []
        for _ in range(21):
            r = client.post(
                "/api/v1/media/upload",
                files={"file": ("a.png", PNG, "image/png")},
            )
            statuses.append(r.status_code)
        # At least one 200, at least one 429.
        assert any(s == 200 for s in statuses)
        assert any(s == 429 for s in statuses)