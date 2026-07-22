"""Health check endpoint — returns status of all infrastructure components."""

import asyncio
from datetime import datetime

from fastapi import APIRouter

from niki.config.settings import get_settings
from niki.infrastructure.chroma import ChromaClient
from niki.infrastructure.neo4j import Neo4jClient
from niki.infrastructure.postgres import PostgresClient
from niki.infrastructure.redis import RedisClient
from niki.schemas.common import HealthResponse, HealthStatus

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Check the health of all infrastructure components.

    Returns:
        HealthResponse with overall status and per-component details.
    """
    settings = get_settings()

    # Check all components concurrently
    results = await asyncio.gather(
        _check("redis", RedisClient.health_check()),
        _check("chromadb", _run_sync_check(ChromaClient.health_check)),
        _check("neo4j", Neo4jClient.health_check()),
        _check("postgresql", PostgresClient.health_check()),
        _check("deepseek", _check_llm_health("DeepSeek-V4")),
        _check("doubao", _check_llm_health("doubao")),
        return_exceptions=True,
    )

    components: list[HealthStatus] = []
    for result in results:
        if isinstance(result, Exception):
            components.append(HealthStatus(component="unknown", status="unhealthy", error=str(result)))
        else:
            components.append(result)

    # Overall status: healthy if all components are healthy
    statuses = [c.status for c in components]
    if all(s == "healthy" for s in statuses):
        overall = "healthy"
    elif any(s == "unhealthy" for s in statuses):
        overall = "unhealthy"
    else:
        overall = "degraded"

    return HealthResponse(
        status=overall,
        version=settings.api_version,
        timestamp=datetime.now(),
        components=components,
    )


@router.get("/health/live")
async def liveness_check() -> dict[str, str]:
    """Kubernetes-style liveness probe — just checks the process is alive."""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness_check() -> dict[str, str | bool]:
    """Kubernetes-style readiness probe — checks if the app can serve traffic."""
    try:
        await RedisClient.health_check()
        await PostgresClient.health_check()
        return {"status": "ready", "ready": True}
    except Exception:
        return {"status": "not_ready", "ready": False}


async def _check(
    name: str,
    coro: object,
) -> HealthStatus:
    """Run a health check coroutine or awaitable and return a HealthStatus."""
    try:
        if asyncio.iscoroutine(coro) or asyncio.isfuture(coro):
            result = await coro
        elif callable(coro):
            result = await asyncio.to_thread(coro)
        else:
            result = coro
        if isinstance(result, dict) and result.get("status") in ("healthy", "degraded", "unhealthy"):
            return HealthStatus(
                component=name,
                status=result["status"],
                latency_ms=result.get("latency_ms"),
                error=result.get("error"),
            )
        return HealthStatus(component=name, status="unhealthy", error="Health check returned no status")
    except Exception as exc:
        return HealthStatus(component=name, status="unhealthy", error=str(exc))


def _run_sync_check(check_fn: object) -> object:
    """Wrap a sync health-check function so _check will dispatch it to a thread."""
    return check_fn


async def _check_llm_health(provider: str) -> dict[str, object]:
    """Check LLM provider health (placeholder — actual check needs API key)."""
    settings = get_settings()

    api_key = (
        settings.deepseek_api_key if provider == "DeepSeek-V4" else settings.doubao_api_key
    )
    if not api_key:
        return {"status": "degraded", "latency_ms": None, "error": "API key not configured"}

    # In a real deployment, do a lightweight API call here
    return {"status": "healthy", "latency_ms": None}
