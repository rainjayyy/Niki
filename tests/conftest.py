"""Shared pytest fixtures for Niki tests."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from niki.auth.account import ParentAccount
from niki.config.settings import Settings, get_settings
from niki.middleware.rate_limit import reset_for_tests
from niki.main import create_app


@pytest.fixture(scope="session")
def event_loop() -> Any:
    """Create a session-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Provide application settings for tests.

    Override with test-specific values.
    """
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> None:
    """Clear the in-process slowapi limiter between tests so that
    cross-test accumulation (e.g. 4 fixture calls to /sms/login
    across 4 tests tripping the 10/minute cap) doesn't cause flakes."""
    reset_for_tests()
    yield
    reset_for_tests()


# Endpoints that import `get_current_parent` and bind it locally.
# To globally override the dependency without rewriting each test to
# mint a JWT, we patch the function on every module that imported it
# plus the source module.
_PROTECTED_MODULES = (
    "niki.auth.dependencies",
    "niki.api.dashboard",
    "niki.api.memory",
    "niki.api.emotion",
    "niki.api.treehole",
    "niki.api.content_policy",
    "niki.api.tools",
    "niki.api.session",
    "niki.api.media",
    "niki.api.audio",
    "niki.api.chat_stream",
)


async def _fake_current_parent() -> ParentAccount:
    """Sentinel returned to every Depends(get_current_parent) call.

    Replaces the real JWT-bearing dep so existing tests don't have
    to mint tokens. New BE-AUTH-001-rollout tests verify the real
    flow separately.
    """
    return ParentAccount(
        id="test_parent",
        name="Test Parent",
        id_number_masked="****",
        phone_masked="****",
    )


@pytest.fixture(autouse=True)
def _fake_auth_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every `create_app()` to install a sentinel parent dep.

    Sets `NIKI_TEST_FAKE_AUTH=1` env var so `niki.main.create_app`
    auto-installs `app.dependency_overrides[get_current_parent]`.
    FastAPI checks overrides at request time so it overrides the
    function reference captured at decoration time. Zero change to
    any test file needed.

    The new `tests/auth/test_protected_routes.py` unsets this env var
    via `monkeypatch.delenv` to exercise the real JWT flow.
    """
    monkeypatch.setenv("NIKI_TEST_FAKE_AUTH", "1")
    yield


@pytest.fixture
def app():
    """Create a FastAPI test application. Function scope so the
    autouse _fake_auth_override has a chance to patch create_app
    before each app instance is constructed."""
    return create_app()


@pytest.fixture
def client(app) -> TestClient:
    """Create a synchronous FastAPI TestClient."""
    return TestClient(app)


@pytest.fixture
async def async_client(app) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
