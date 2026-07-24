"""Shared pytest fixtures for Niki tests."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

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


@pytest.fixture(scope="module")
def app():
    """Create a FastAPI test application."""
    return create_app()


@pytest.fixture(scope="module")
def client(app) -> TestClient:
    """Create a synchronous FastAPI TestClient."""
    return TestClient(app)


@pytest.fixture(scope="module")
async def async_client(app) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
