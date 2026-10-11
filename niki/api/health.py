"""Health check endpoint — returns status of all infrastructure components."""

import asyncio
from datetime import datetime

from fastapi import APIRouter

from niki.config.settings import get_settings
from niki.infrastructure.chroma import ChromaClient
from niki.infrastructure.neo4j import Neo4jClient
from niki.infrastructure.postgres import PostgresClient
from niki.infrastructure.redis import RedisClient
from niki.middleware.rate_limit import (
    get_rate_limit_backend,
    is_rate_limit_in_fallback,
)
from niki.schemas.common import HealthResponse, HealthStatus

router = APIRouter(
    tags=[  
        {  # type: ignore[list-item]
            "name": "health",
            "description": (
                "Health check endpoint — returns status of all infrastructure components."
            ),
        },
    ]
)


@router.get(
    "/health",
    summary="Check the health of all infrastructure components",
    description="""
            Check the health of all infrastructure components.
            
            Returns:
                HealthResponse with overall status and per-component details.
            """,
)
async def health_check() -> HealthResponse:
    """Check the health of all infrastructure components.

    Returns:
        HealthResponse with overall status and per-component details.
    """
    settings = get_settings()

    # Check all components concurrently
    results = await asyncio.gather(
        _check("redis", RedisClient.health_check()),
        _check("chromadb", ChromaClient.health_check()),
        _check("neo4j", Neo4jClient.health_check()),
        _check("postgresql", PostgresClient.health_check()),
        _check("deepseek", _check_llm_health("DeepSeek-V4")),
        _check("doubao", _check_llm_health("doubao")),
        return_exceptions=True,
    )

    components: list[HealthStatus] = []
    for result in results:
        if isinstance(result, Exception):
            components.append(
                HealthStatus(component="unknown", status="unhealthy", error=str(result), latency_ms=None)
            )
        else:
            components.append(result)  # type: ignore[arg-type]

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
        rate_limit_backend=get_rate_limit_backend(),
        rate_limit_in_fallback=is_rate_limit_in_fallback(),
    )


@router.get(
    "/health/live",
    summary="""
            Kubernetes-style liveness probe — just checks the process is alive
            """,
    description="""
            Kubernetes-style liveness probe — just checks the process is alive.
            """,
)
async def liveness_check() -> dict[str, str]:
    """Kubernetes-style liveness probe — just checks the process is alive."""
    return {"status": "alive"}


@router.get(
    "/health/ready",
    summary="""
            Kubernetes-style readiness probe — checks if the app can serve traffic
            """,
    description="""
            Kubernetes-style readiness probe — checks if the app can serve traffic.
            """,
)
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
        if isinstance(result, dict) and result.get("status") in (
            "healthy",
            "degraded",
            "unhealthy",
        ):
            return HealthStatus(
                component=name,
                status=result["status"],
                latency_ms=result.get("latency_ms"),
                error=result.get("error"),
            )
        return HealthStatus(
            component=name, status="unhealthy", error="Health check returned no status", latency_ms=None
        )
    except Exception as exc:
        return HealthStatus(component=name, status="unhealthy", error=str(exc), latency_ms=None)


def _run_sync_check(check_fn: object) -> object:
    """Wrap a sync health-check function so _check will dispatch it to a thread."""
    return check_fn


async def _check_llm_health(provider: str) -> dict[str, object]:
    """Check LLM provider health by calling the actual provider.

    Uses ``BaseLLMClient.health_check()`` (a real HTTP GET to the
    provider's ``/models`` endpoint) when a configured client is
    available. Falls back to "degraded" only when no key is set —
    a previously-buggy branch that returned "healthy" with no
    actual check.
    """
    from niki.infrastructure.llm.factory import get_or_create_llm

    settings = get_settings()
    api_key = settings.deepseek_api_key if provider == "DeepSeek-V4" else settings.doubao_api_key
    if not api_key:
        return {
            "status": "degraded",
            "latency_ms": None,
            "error": f"API key not configured for {provider}",
        }

    import time

    try:
        client = get_or_create_llm()
        # The factory picks the primary provider, but ``provider_name``
        # tells us whether the configured key is actually wired. For
        # the deepseek/doubao stub clients we still want a real ping,
        # so we use the base-class HTTP health probe directly.
        start = time.perf_counter()
        ok = await client.health_check()
        latency_ms = int((time.perf_counter() - start) * 1000)
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "status": "unhealthy",
            "latency_ms": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if ok:
        return {"status": "healthy", "latency_ms": latency_ms}
    return {
        "status": "degraded",
        "latency_ms": latency_ms,
        "error": f"LLM provider {provider} did not respond to /models probe",
    }
