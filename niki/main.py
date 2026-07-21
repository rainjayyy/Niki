"""Niki — FastAPI application entry point.

Start the application:
    uvicorn niki.main:app --reload
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from niki.api.router import api_router
from niki.config.logging_config import configure_logging
from niki.config.settings import get_settings
from niki.infrastructure.chroma import ChromaClient
from niki.infrastructure.neo4j import Neo4jClient
from niki.infrastructure.postgres import PostgresClient
from niki.infrastructure.redis import RedisClient
from niki.middleware.cors import setup_cors
from niki.middleware.error_handler import setup_error_handlers

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

    logger.info("app.started")

    # ===== Yield to application =====
    yield

    # ===== Shutdown =====
    logger.info("app.stopping")

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
    )

    # Middleware
    setup_cors(app)
    setup_error_handlers(app)

    # Routes
    app.include_router(api_router)

    return app


# Application instance
app = create_app()
