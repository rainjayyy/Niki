"""Rate-limit middleware (BE-API-002).

Provides a slowapi-backed `limiter` that:
  - uses `request.client.host` as the default key (per-IP);
  - allows per-route opt-in via `@limiter.limit("N/period")`;
  - raises 429 on overflow with §6.3 response shape.

NOTE: We deliberately DO NOT install `SlowAPIMiddleware` because
slowapi 0.1.10 is incompatible with Starlette >= 0.40 — the middleware's
header injection logic (`inject_headers`) tries to mutate a Response
that's no longer passed in via the expected positional argument,
raising `Exception('parameter response must be an instance of
starlette.responses.Response')`. Per-route decorators bypass that
path entirely, so we still get enforcement; we just lose the
auto-default 100/min cap. New endpoints can opt-in explicitly.

429 response body (matches BE-API-001 error contract):
    {
      "success": false,
      "error": {
        "code": "RATE_LIMIT_EXCEEDED",
        "message": "...",
        "detail": {"limit": "30/minute", "retry_after_seconds": N}
      },
      "error_code": "RATE_LIMIT_EXCEEDED",
      "request_id": "uuid-v4"
    }

BE-API-002-redis:
    Storage backend is configurable via `rate_limit_storage_uri`
    in Settings. Default `memory://` keeps the legacy in-process
    behaviour. Production sets
    `NIKI_RATE_LIMIT_STORAGE_URI=redis://host:6379/1` so multiple
    uvicorn workers share a single counter. If Redis is
    unreachable, slowapi's `in_memory_fallback_enabled=True`
    degrades gracefully to local memory rather than 500ing.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from niki.config.settings import get_settings
from niki.metrics import get_registry
from niki.schemas.common import APIResponse

logger = structlog.get_logger(__name__)


def _build_limiter() -> Limiter:
    """Build the process-wide Limiter.

    `headers_enabled=False` — slowapi's per-response header injection
    is broken against Starlette >= 0.40 (raises "parameter response
    must be an instance of starlette.responses.Response"). We still
    return Retry-After / X-RateLimit-* headers in our custom 429
    handler, so the API contract holds.

    BE-API-002-redis: `storage_uri` lets multiple uvicorn workers
    share a counter via Redis. `in_memory_fallback_enabled=True`
    guarantees a Redis outage doesn't take the whole API down —
    it just degrades to per-process memory for the rest of the
    process lifetime, with a warning logged.
    """
    settings = get_settings()
    return Limiter(
        key_func=get_remote_address,
        headers_enabled=False,
        strategy="fixed-window",
        storage_uri=settings.rate_limit_storage_uri,
        in_memory_fallback_enabled=True,
    )


limiter: Limiter = _build_limiter()


def get_limiter() -> Limiter:
    """Return the process-wide Limiter (used by route decorators)."""
    return limiter


def get_rate_limit_backend() -> str:
    """Return a short label for the active storage backend.

    `redis` when `rate_limit_storage_uri` points at Redis,
    `memory` otherwise. Used by the /health endpoint so ops can
    see whether we're currently distributed or per-process.
    """
    uri = get_settings().rate_limit_storage_uri
    if uri.startswith("redis://") or uri.startswith("rediss://"):
        return "redis"
    return "memory"


def is_rate_limit_in_fallback() -> bool:
    """True if slowapi has degraded to its in-memory fallback.

    slowapi exposes this via `limiter._storage` after a Redis
    outage; once it's flipped to the fallback storage it stays
    there for the rest of the process lifetime.
    """
    if get_rate_limit_backend() != "redis":
        return False
    storage = getattr(limiter, "_storage", None)
    if storage is None:
        return False
    # slowapi names the in-memory fallback `MemoryStorage`.
    return type(storage).__name__ == "MemoryStorage"


def setup_rate_limit(app: FastAPI) -> None:
    """Register a 429 handler that maps to the §6.3 error shape."""
    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def _on_rate_limit(
        request: Request, exc: RateLimitExceeded
    ) -> JSONResponse:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        limit_str = str(exc.detail) if hasattr(exc, "detail") else "unknown"
        retry_after = 60
        logger.warning(
            "rate_limit_exceeded",
            request_id=request_id,
            client_ip=request.client.host if request.client else "unknown",
            path=request.url.path,
            limit=limit_str,
        )
        # BE-API-009: count 429s by endpoint.
        try:
            get_registry().RATE_LIMITED_TOTAL.labels(
                endpoint=request.url.path,
            ).inc()
        except Exception:  # pragma: no cover
            pass
        body = APIResponse.from_error(
            code="RATE_LIMIT_EXCEEDED",
            message=f"Too many requests (limit: {limit_str})",
            request_id=request_id,
            detail={"limit": limit_str, "retry_after_seconds": retry_after},
        ).model_dump()
        return JSONResponse(
            status_code=429,
            content=body,
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": limit_str,
                "X-RateLimit-Remaining": "0",
                "X-Request-ID": request_id,
            },
        )


def reset_for_tests() -> None:
    """Clear in-memory rate-limit storage between tests.

    When the limiter is configured with a Redis URI in tests
    (rare — usually production-only), the underlying storage
    implements `.reset()` too, so the same call works.
    """
    if hasattr(limiter, "reset"):
        limiter.reset()