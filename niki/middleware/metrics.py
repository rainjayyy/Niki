"""HTTP request latency middleware (BE-API-009).

Observes ``niki_http_request_duration_seconds{method, endpoint}``
on every HTTP request handled by FastAPI. The endpoint label is the
FastAPI route **template** (e.g. ``/api/v1/children/{child_id}``),
not the raw URL path — otherwise user-supplied values like phone
numbers or UUIDs would explode label cardinality and crash Prometheus.

Behaviour:

- Skips the scrape target (``/api/v1/metrics``) so that the act of
  scraping itself doesn't bloat the histogram. Scrapes happen every
  15-30s and would otherwise dominate the latency distribution.
- Skips ``/openapi.json`` and ``/docs`` for the same reason —
  documentation fetches shouldn't pollute production metrics.
- Records the timing AFTER the response is returned so 5xx latencies
  are captured too.
- Exceptions raised by the downstream handler propagate normally;
  we still observe the partial elapsed time so failed requests show
  up in the histogram (callers can correlate with response status).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from niki.metrics import get_registry

# Paths we never want to observe in the latency histogram — they're
# either scrape traffic or docs fetches that would dominate samples.
_SKIP_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/metrics",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)


def _resolve_endpoint_template(request: Request) -> str:
    """Return the FastAPI route template for ``request`` if matched.

    Falls back to the raw path when the request didn't match a
    route (404 for unknown URL). The fallback still goes through
    the histogram because that bucket is informative ("we got a
    404 for path X"), but the label cardinality stays bounded by
    the number of distinct unknown URLs (which we accept as a
    known-small number per typical web traffic).
    """
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path  # type: ignore[no-any-return]
    return request.url.path


class MetricsMiddleware(BaseHTTPMiddleware):
    """Per-request latency observer for the HTTP_REQUEST_DURATION histogram."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in _SKIP_PATHS:
            # Bypass entirely — don't even start the timer.
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Record the elapsed time even if downstream raised; the
            # histogram will reflect the failure duration. The exception
            # propagates so FastAPI's default error handler (or our
            # own) can still produce a 500 response.
            elapsed = time.perf_counter() - start
            endpoint = _resolve_endpoint_template(request)
            get_registry().HTTP_REQUEST_DURATION.labels(
                method=request.method,
                endpoint=endpoint,
            ).observe(elapsed)
            raise

        elapsed = time.perf_counter() - start
        endpoint = _resolve_endpoint_template(request)
        get_registry().HTTP_REQUEST_DURATION.labels(
            method=request.method,
            endpoint=endpoint,
        ).observe(elapsed)
        return response
