"""Niki — FastAPI application entry point.

Start the application:
    uvicorn niki.main:app --reload
"""

import mimetypes
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from niki.api.router import OPENAPI_TAGS, api_router
from niki.config.logging_config import configure_logging
from niki.config.settings import get_settings
from niki.i18n.middleware import LanguageMiddleware
from niki.infrastructure.chroma import ChromaClient
from niki.infrastructure.heartbeat import HeartbeatService
from niki.infrastructure.neo4j import Neo4jClient
from niki.infrastructure.postgres import PostgresClient
from niki.infrastructure.redis import RedisClient
from niki.middleware.cors import setup_cors
from niki.middleware.error_handler import setup_error_handlers
from niki.middleware.metrics import MetricsMiddleware
from niki.middleware.min_client_version import (
    MinClientVersionMiddleware,
)
from niki.middleware.rate_limit import setup_rate_limit

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan — startup and shutdown logic."""
    # ===== Startup =====
    configure_logging()
    settings = get_settings()
    logger.info(
        "app.starting",
        app_name=settings.app_name,
        version=settings.api_version,
        debug=settings.debug,
    )

    # Initialize infrastructure connections
    try:
        await RedisClient.get_client()
        logger.info("app.redis_ready")
    except Exception as exc:
        logger.warning("app.redis_unavailable", error=str(exc))

    try:
        PostgresClient.get_engine()
        logger.info("app.postgres_ready")
        from niki.infrastructure.repository_wiring import wire_postgres_repositories

        wire_postgres_repositories()
        logger.info("app.repositories_postgres_wired")
        from niki.services.child_registry import sync_child_registry_from_postgres

        await sync_child_registry_from_postgres()
        logger.info("app.child_registry_loaded")
    except Exception as exc:
        logger.warning("app.postgres_unavailable", error=str(exc))

    try:
        ChromaClient.get_client()
        ChromaClient.ensure_collections()
        logger.info("app.chroma_ready")
    except Exception as exc:
        logger.warning("app.chroma_unavailable", error=str(exc))

    try:
        await Neo4jClient.get_driver()
        logger.info("app.neo4j_ready")
    except Exception as exc:
        logger.warning("app.neo4j_unavailable", error=str(exc))

    # BE-STOR-002: start background heartbeats for the four clients.
    heartbeat = HeartbeatService()
    heartbeat.register("postgres", PostgresClient.health_check, interval_seconds=30)
    heartbeat.register("redis", RedisClient.health_check, interval_seconds=30)
    heartbeat.register("chroma", ChromaClient.health_check, interval_seconds=60)
    heartbeat.register("neo4j", Neo4jClient.health_check, interval_seconds=30)
    # BE-DEV-004: periodic cleanup of expired pairing codes (5-min TTL).
    from niki.infrastructure.heartbeat import cleanup_expired_pairing_codes

    heartbeat.register(
        "pairing_cleanup",
        cleanup_expired_pairing_codes,
        interval_seconds=60,
    )
    # BE-AUTH-006: periodic sweep of phone-change requests whose
    # 7-day cooling period has elapsed. 1 hour is plenty of
    # granularity; the row's `effective_at` is the source of truth.
    from niki.infrastructure.heartbeat import (
        cleanup_completed_phone_changes,
    )

    heartbeat.register(
        "phone_change_sweep",
        cleanup_completed_phone_changes,
        interval_seconds=3600,
    )

    # ISS-11: refuse to boot in production with a stub LLM. The
    # factory falls back to a stub when no real provider
    # credentials are configured — fine for dev / staging, but a
    # P0 incident waiting to happen in production. Trip early
    # with a loud, structured-log message so on-call sees it
    # immediately.
    from niki.infrastructure.llm.factory import is_fallback_mode

    if settings.app_env == "production" and is_fallback_mode():
        logger.error(
            "app.startup_blocked_production_stub_llm",
            note=(
                "app_env=production but the LLM factory fell back to the "
                "stub provider. Set MINIMAX_API_KEY (or DEEPSEEK_API_KEY / "
                "DOUBAO_API_KEY) to a real credential, OR set app_env to "
                "staging / development."
            ),
        )
        raise RuntimeError(
            "Cannot start in production with the stub LLM. "
            "Set a real MINIMAX_API_KEY (or equivalent) and restart."
        )
    if is_fallback_mode():
        logger.warning(
            "app.running_with_stub_llm",
            app_env=settings.app_env,
            note=(
                "No real LLM credentials configured — all responses "
                "come from the deterministic stub. Safe for dev / "
                "staging, NEVER for production."
            ),
        )

    await heartbeat.start()
    app.state.heartbeat = heartbeat

    logger.info("app.started")

    # ===== Yield to application =====
    yield

    # ===== Shutdown =====
    logger.info("app.stopping")

    if hasattr(app.state, "heartbeat"):
        await heartbeat.stop()

    await RedisClient.close()
    await PostgresClient.close()
    await Neo4jClient.close()
    ChromaClient.close()

    logger.info("app.stopped")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        A fully configured FastAPI app instance.
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        description="AI Companion & Growth Assistant for Children aged 3-12",
        version="0.1.0",
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        lifespan=lifespan,
        # BE-API-006: per-tag descriptions aggregated by
        # `niki.api.router._collect_tag_metadata`. Drives the
        # global `tags` block in `/openapi.json` so Swagger UI /
        # ReDoc show a description per group.
        openapi_tags=OPENAPI_TAGS,
    )

    # Test-only: install a fake parent override so existing tests don't
    # need to mint real JWTs. Set NIKI_TEST_FAKE_AUTH=1 in conftest.
    # Production should NEVER set this.
    if os.environ.get("NIKI_TEST_FAKE_AUTH") == "1":
        from niki.auth.account import ParentAccount
        from niki.auth.dependencies import get_current_parent

        async def _fake_parent() -> ParentAccount:
            return ParentAccount(
                id="test_parent",
                name="Test Parent",
                id_number_masked="****",
                phone_masked="****",
            )

        app.dependency_overrides[get_current_parent] = _fake_parent

    # Middleware
    setup_cors(app)
    setup_error_handlers(app)
    setup_rate_limit(app)

    # BE-I18N-001: stash the resolved language on `request.state.lang`
    # so handlers can render localized error messages via the
    # `Translator`. Runs after CORS so the response `Vary` header is
    # added to CORS-decorated responses too.
    app.add_middleware(LanguageMiddleware)

    # BE-API-009: observe HTTP request latency into
    # `niki_http_request_duration_seconds{method, endpoint}` for every
    # routed request. Added last so it sits on the outermost shell —
    # that way it captures the full middleware stack latency.
    app.add_middleware(MetricsMiddleware)

    # BE-COMP-003: reject clients older than the minimum
    # supported version. Reads `X-Client-Version`.
    app.add_middleware(
        MinClientVersionMiddleware,
        min_supported_version=settings.min_supported_version,
    )

    # Routes
    app.include_router(api_router)

    # BE-PUSH-003: static assets (Web Push service worker + the
    # subscription helper the parent-facing app imports). Served from
    # the package directory so it works the same whether the app is
    # started from the repo root or from an installed wheel.
    _mount_static(app)

    return app


# Windows maps `.js` to `text/plain` in some registry configurations,
# and a browser refuses to execute a service worker that isn't served
# with a JavaScript MIME type. Pin it at import time.
mimetypes.add_type("application/javascript", ".js")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class _StaticFilesWithSWScope(StaticFiles):
    """StaticFiles that lets the service worker claim the root scope.

    A worker served from `/static/` may only control `/static/` unless
    the response carries `Service-Worker-Allowed: /`. Without this
    header, `register(url, {scope: '/'})` throws and the parent never
    receives a push for any page outside `/static/`.
    """

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        if path.endswith("service-worker.js"):
            response.headers["Service-Worker-Allowed"] = "/"
        return response


def _mount_static(app: FastAPI) -> None:
    """Mount `niki/static/` at `/static`.

    Missing directory is not fatal: a deployment that strips static
    assets should still serve the API. We log and move on.
    """
    if not STATIC_DIR.is_dir():  # pragma: no cover — packaging accident
        logger.warning("app.static_dir_missing", path=str(STATIC_DIR))
        return
    app.mount(
        "/static",
        _StaticFilesWithSWScope(directory=str(STATIC_DIR)),
        name="static",
    )


# Application instance
app = create_app()
