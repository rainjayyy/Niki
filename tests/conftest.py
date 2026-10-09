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

# Pre-register common test (parent, child) pairs so existing
# dashboard / treehole tests work with the new ownership check.
# In production this is replaced by Postgres-backed ChildRepository.
COMMON_TEST_CHILDREN = {
    "test_parent": ["test-child-1", "test-child-2", "child-A", "child-B", "child-001", "child-X"],
    "parent_demo": ["child-demo-1"],
    "parent-1": ["child-1", "child-2", "child-A", "child-B", "child-001"],
    "test_parent": ["test-child-1", "test-child-2", "child-A", "child-B", "child-001", "child-X", "child-2"],
    "parent-A": ["child-A1", "child-A2"],
    "parent-B": ["child-B1"],
}


async def _instant_sleep(_seconds: float) -> None:
    """No-op `asyncio.sleep` substitute for the webhook dispatcher.

    The production dispatcher sleeps 1s/4s/16s between retries.
    Tests that don't care about the wall-clock duration want
    the dispatcher to proceed immediately; tests that *do*
    care inject their own captor.
    """
    return None

@pytest.fixture(autouse=True)
def _reset_session_store():
    """BE-SESSION-redis: reset the session store singleton
    between tests so cross-test session state doesn't leak."""
    from niki.session.store import (
        InMemorySessionStore,
        set_session_store,
    )
    set_session_store(InMemorySessionStore())
    yield
    set_session_store(None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _register_common_test_children():
    """Auto-register common (parent, child) pairs so the ownership
    dep lets existing tests through. Tests can override via
    set_child_registry(InMemoryChildRegistry()) and register_child()
    manually for custom scenarios."""
    from niki.services.child_registry import InMemoryChildRegistry, set_child_registry
    registry = InMemoryChildRegistry()
    for parent, children in COMMON_TEST_CHILDREN.items():
        for child in children:
            registry.register(child, parent)
    set_child_registry(registry)

    # BE-TREEHOLE-AGG: also reset the aggregate repo so the
    # cross-child rollup in `aggregate_for_baseline(child_id=None)`
    # doesn't accumulate state across tests.
    from niki.db.repositories import (
        InMemoryTreeholeAggregationRepository,
        InMemoryPushDeviceRepository,
        InMemoryPushPrefsRepository,
        set_push_device_repository,
        set_push_prefs_repository,
        set_treehole_aggregation_repository,
        set_treehole_repository,
    )
    from niki.db.repositories import InMemoryTreeholeRepository
    from niki.db.repositories import (
        InMemoryWebhookRepository,
        set_webhook_repository,
    )
    from niki.services.webhook import (
        WebhookDispatcherService,
        set_webhook_dispatcher,
    )
    set_treehole_repository(InMemoryTreeholeRepository())
    set_treehole_aggregation_repository(
        InMemoryTreeholeAggregationRepository()
    )
    # BE-PUSH-001: reset push device + prefs repos too
    set_push_device_repository(InMemoryPushDeviceRepository())
    set_push_prefs_repository(InMemoryPushPrefsRepository())
    # BE-API-008: reset webhook repo + dispatcher. We pass the
    # SAME repo instance to the dispatcher that we set as the
    # singleton, so writes through either the API or the
    # dispatcher hit the same backing store (otherwise a
    # delivery created via the dispatcher is invisible to a
    # direct repo.list_* call, and vice versa).
    _webhook_repo = InMemoryWebhookRepository()
    set_webhook_repository(_webhook_repo)
    set_webhook_dispatcher(
        WebhookDispatcherService(
            repo=_webhook_repo,
            sleep=_instant_sleep,
        )
    )


@pytest.fixture(autouse=True)
def _reset_auth_audit():
    """Reset the auth-audit log repo + logger between tests so
    cross-test audit events don't leak. Tests that want to inspect
    audit output can override via set_auth_audit_log_repository()
    in their own setup."""
    from niki.db.repositories import (
        InMemoryAuthAuditLogRepository,
        set_auth_audit_log_repository,
    )
    from niki.security import (
        ConsoleAlertDispatcher,
        ViolationRateTracker,
        set_alert_dispatcher,
    )
    from niki.services.auth_audit import (
        AuthAuditLogger,
        set_auth_audit_logger,
    )

    repo = InMemoryAuthAuditLogRepository()
    set_auth_audit_log_repository(repo)
    tracker = ViolationRateTracker(
        window_seconds=300, burst_count=3
    )
    set_alert_dispatcher(ConsoleAlertDispatcher())
    set_auth_audit_logger(
        AuthAuditLogger(
            repo=repo,
            rate_tracker=tracker,
            dispatcher=ConsoleAlertDispatcher(),
        )
    )



@pytest.fixture(autouse=True)
def _reset_unified_audit():
    """BE-COMP-004: reset the unified `audit_logs` repo + service
    between tests. Every domain now dual-writes here, so without a
    reset a test asserting "exactly one safety_block row" would see
    rows accumulated by the whole session."""
    from niki.db.repositories import (
        InMemoryAuditLogRepository,
        set_audit_log_repository,
    )
    from niki.services.audit_log import (
        AuditLogService,
        set_audit_log_service,
    )

    repo = InMemoryAuditLogRepository()
    set_audit_log_repository(repo)
    set_audit_log_service(AuditLogService(repo=repo))


@pytest.fixture(autouse=True)
def _reset_letter_repo():
    """Reset the in-memory letter store between tests."""
    from niki.db.repositories.letter_repo import (
        InMemoryLetterRepository,
        set_letter_repository,
    )

    set_letter_repository(InMemoryLetterRepository())
    yield
    set_letter_repository(InMemoryLetterRepository())


@pytest.fixture(autouse=True)
def _reset_short_term_memory():
    """Reset the process-wide short-term memory between tests."""
    from niki.memory.short_term import (
        ShortTermMemory,
        set_short_term_memory,
    )

    set_short_term_memory(ShortTermMemory())
    yield
    set_short_term_memory(None)


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


@pytest.fixture(autouse=True)
def _reset_metrics_registry() -> None:
    """BE-API-009: drop the Prometheus registry between tests so
    counter values can't leak across tests."""
    from niki.metrics import reset_for_tests as reset_metrics

    reset_metrics()
    yield
    reset_metrics()


@pytest.fixture(autouse=True)
def _reset_llm_factory() -> None:
    """Drop the process-wide LLM client + streaming provider singletons
    between tests so a test that injects a mock/stub doesn't leak into
    the next test (the factory caches the first provider it builds).

    The factory re-resolves on the next `get_or_create_llm()` /
    `get_streaming_provider()` call, picking up whatever env is set.
    """
    from niki.infrastructure.llm.factory import (
        reset_for_tests as reset_llm_factory,
    )
    from niki.infrastructure.llm.streaming import (
        reset_for_tests as reset_streaming,
    )

    reset_llm_factory()
    reset_streaming()
    yield
    reset_llm_factory()
    reset_streaming()


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
