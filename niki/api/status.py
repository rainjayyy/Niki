"""Public status endpoint (BE-MISC-003).

Returns the overall service health + per-dependency status.
Unlike `/api/v1/health` (operator-facing, requires no auth but
returns detailed metrics), this is a coarse public view safe
to expose to the marketing site, etc.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

import structlog

from niki.config.settings import Settings, get_settings
from niki.infrastructure.chroma import ChromaClient
from niki.infrastructure.neo4j import Neo4jClient
from niki.infrastructure.postgres import PostgresClient
from niki.infrastructure.redis import RedisClient

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="", tags=[{"name": "status", "description": "Public status endpoint (BE-MISC-003)."}]
)


@router.get(
    "/status",
    summary="Coarse-grained health for the public status page",
    description="""
            Coarse-grained health for the public status page.
            
            Returns 200 even when individual deps are degraded —
            consumers (the marketing site, the support dashboard)
            decide what to do with the body.
            """,
)
async def public_status(
    settings: Settings = Depends(get_settings),
) -> dict:
    """Coarse-grained health for the public status page.

    Returns 200 even when individual deps are degraded —
    consumers (the marketing site, the support dashboard)
    decide what to do with the body.
    """
    # Concurrently probe each dependency. The /health endpoint
    # already wraps these in asyncio.gather; we reuse the
    # underlying check helpers.
    results = await asyncio.gather(
        _check_redis(),
        _check_postgres(),
        _check_chroma(),
        _check_neo4j(),
        return_exceptions=True,
    )
    components = []
    all_healthy = True
    for name, result in zip(["redis", "postgres", "chroma", "neo4j"], results):
        if isinstance(result, Exception):
            components.append(
                {
                    "name": name,
                    "status": "unhealthy",
                    "error": str(result),
                }
            )
            all_healthy = False
        else:
            components.append({"name": name, **result})
            if result.get("status") != "healthy":
                all_healthy = False

    return {
        "status": "healthy" if all_healthy else "degraded",
        "version": settings.api_version,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "components": components,
    }


async def _check_redis() -> dict:
    try:
        r = await RedisClient.health_check()
        return r
    except Exception as exc:
        return {"name": "redis", "status": "unhealthy", "error": str(exc)}


async def _check_postgres() -> dict:
    try:
        r = await PostgresClient.health_check()
        return r
    except Exception as exc:
        return {"name": "postgres", "status": "unhealthy", "error": str(exc)}


async def _check_chroma() -> dict:
    try:
        # ChromaClient.health_check is async (as of the heartbeat fix);
        # await it directly — it uses httpx.AsyncClient internally.
        r = await ChromaClient.health_check()
        return r
    except Exception as exc:
        return {"name": "chroma", "status": "unhealthy", "error": str(exc)}


async def _check_neo4j() -> dict:
    try:
        r = await Neo4jClient.health_check()
        return r
    except Exception as exc:
        return {"name": "neo4j", "status": "unhealthy", "error": str(exc)}
