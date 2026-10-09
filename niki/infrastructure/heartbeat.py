"""Background heartbeat service for infrastructure clients (BE-STOR-002).

Each registered client runs an async `ping` function on a recurring
schedule. Failures are caught and stored as `unhealthy` results — they
NEVER crash the loop or affect other clients.

Usage:
    service = HeartbeatService()
    service.register("postgres", PostgresClient.health_check, interval_seconds=30)
    service.register("redis",    RedisClient.health_check,    interval_seconds=30)
    await service.start()
    ...
    await service.stop()

The service is async-task driven — start() spawns N tasks (one per
registered checker), stop() cancels them cleanly and awaits done.

Snapshot:
    aggregate_health() returns the most recent check result per
    client: {name: {status, latency_ms, error?, checked_at}}.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class _Registration:
    name: str
    ping_fn: Callable[[], Awaitable[dict[str, Any]]]
    interval_seconds: float
    task: asyncio.Task[None] | None = None


class HeartbeatService:
    """Coordinate periodic ping tasks across infrastructure clients."""

    def __init__(self) -> None:
        self._registrations: list[_Registration] = []
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._snapshots_lock = asyncio.Lock()
        self._started = False

    def register(
        self,
        name: str,
        ping_fn: Callable[[], Awaitable[dict[str, Any]]],
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        """Register a client checker; must be called BEFORE start().

        Args:
            name: Short identifier (e.g. "postgres", "redis").
            ping_fn: Async function returning a dict with keys
                ``status`` ("healthy" | "unhealthy"),
                ``latency_ms`` (float | None),
                and optionally ``error`` (str).
            interval_seconds: How often to invoke ping_fn.
        """
        if self._started:
            raise RuntimeError(
                "Cannot register after start(); "
                "build the service, register all checkers, then start()."
            )
        # Replace existing registration with the same name (last write wins).
        self._registrations = [r for r in self._registrations if r.name != name]
        self._registrations.append(
            _Registration(
                name=name,
                ping_fn=ping_fn,
                interval_seconds=interval_seconds,
            )
        )

    async def start(self) -> None:
        """Spawn one background task per registered checker."""
        if self._started:
            return
        self._started = True
        for reg in self._registrations:
            reg.task = asyncio.create_task(
                self._run_loop(reg),
                name=f"heartbeat:{reg.name}",
            )
        logger.info(
            "heartbeat.started",
            count=len(self._registrations),
            names=[r.name for r in self._registrations],
        )

    async def stop(self) -> None:
        """Cancel all background tasks and await their completion."""
        if not self._started:
            return
        self._started = False
        tasks = [r.task for r in self._registrations if r.task is not None]
        for t in tasks:
            t.cancel()
        # Shield: gather swallows CancelledError, we still want each task
        # to finish unwinding its finally clause before stop() returns.
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("heartbeat.stopped")

    async def aggregate_health(self) -> dict[str, dict[str, Any]]:
        """Return the most recent per-checker snapshot.

        Returns a deep copy so callers can mutate the result without
        poisoning internal state.
        """
        import copy

        async with self._snapshots_lock:
            return copy.deepcopy(self._snapshots)

    # ---- internals ----

    async def _run_loop(self, reg: _Registration) -> None:
        """Single checker's lifetime loop with try/except isolation."""
        try:
            while True:
                await self._check_once(reg)
                await asyncio.sleep(reg.interval_seconds)
        except asyncio.CancelledError:
            logger.debug("heartbeat.loop_cancelled", name=reg.name)
            raise
        except Exception as exc:  # belt-and-braces — never propagate
            logger.error(
                "heartbeat.loop_crashed",
                name=reg.name,
                error=str(exc),
            )

    async def _check_once(self, reg: _Registration) -> None:
        """Single ping attempt; never raises."""
        try:
            result = await reg.ping_fn()
        except Exception as exc:
            result = {
                "status": "unhealthy",
                "latency_ms": None,
                "error": f"ping raised: {exc}",
            }
        # Normalise: ensure status/latency_ms/checked_at always present.
        result.setdefault("status", "unknown")
        result.setdefault("latency_ms", None)
        result["checked_at"] = time.time()
        async with self._snapshots_lock:
            self._snapshots[reg.name] = result
        if result["status"] != "healthy":
            logger.warning(
                "heartbeat.check_failed",
                name=reg.name,
                error=result.get("error"),
            )


# ---------------------------------------------------------------------------
# Periodic maintenance coroutine (BE-DEV-004)
# ---------------------------------------------------------------------------


async def cleanup_expired_pairing_codes() -> dict[str, Any]:
    """Drop expired entries from the in-memory pairing code store.

    Designed to be registered with `HeartbeatService.register(...)`
    the same way infrastructure health checks are. Returns a
    `ping_fn`-shaped dict so it slots into the heartbeat loop
    without any special-casing.

    Errors are swallowed (matching the heartbeat contract) and
    surfaced via the `error` field so a transient failure never
    crashes the loop.
    """
    from niki.auth.pairing import get_pairing_store

    started = time.monotonic()
    try:
        removed = get_pairing_store().cleanup_expired()
    except Exception as exc:
        return {
            "status": "unhealthy",
            "latency_ms": None,
            "error": f"cleanup raised: {exc}",
        }
    latency_ms = (time.monotonic() - started) * 1000.0
    return {
        "status": "healthy",
        "latency_ms": latency_ms,
        "removed": removed,
    }


async def cleanup_completed_phone_changes() -> dict[str, Any]:
    """BE-AUTH-006: sweep phone-change requests whose cooling period
    has elapsed and complete them.

    Registered with `HeartbeatService` from `niki.main.lifespan`. The
    interval matches the cooling window's smallest meaningful
    granularity: 1 hour is more than enough for a 7-day window.
    """
    from niki.services.phone_change import get_phone_change_service

    started = time.monotonic()
    try:
        completed = await get_phone_change_service().sweep_expired()
    except Exception as exc:
        return {
            "status": "unhealthy",
            "latency_ms": None,
            "error": f"phone_change sweep raised: {exc}",
        }
    latency_ms = (time.monotonic() - started) * 1000.0
    return {
        "status": "healthy",
        "latency_ms": latency_ms,
        "detail": {"completed": completed},
    }
