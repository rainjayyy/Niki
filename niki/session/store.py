"""SessionStore abstraction (BE-SESSION-redis).

Holds the four pieces of per-child session state that used to
live as dicts on `SessionLimiter`:

    - started_at  : datetime the current session started
    - last_activity : datetime of the most recent message
    - soft_warned  : bool — has the 5-min warning been issued?
    - ended       : str | None — ExitReason value

Two implementations:

    InMemorySessionStore   — process-local dicts (dev / single-process)
    RedisSessionStore      — S3-compatible (Aliyun OSS / AWS ElastiCache
                             / Tencent CRS), shared across workers
    FallbackSessionStore  — wraps a primary store; on Redis failure
                            degrades to in-memory for the rest of the
                            process (and the in_fallback flag lets
                            the /health endpoint surface it)
"""

from __future__ import annotations

import asyncio
import json
import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# Default TTL = 24h. Sessions are scoped to a calendar day so
# the next day's start_session() naturally replaces any stale
# state from the day before.
DEFAULT_TTL_SECONDS: int = 24 * 60 * 60


def _iso(dt: datetime) -> str:
    """Serialize an aware datetime as ISO 8601 with 'Z'."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(s: str) -> datetime:
    """Inverse of `_iso`. Returns aware UTC datetime."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class SessionStore(ABC):
    """Contract for per-child session state.

    Methods are intentionally fine-grained so Redis can use
    short-TTL keys per field, and InMemory can use plain dicts.
    """

    @abstractmethod
    def get_started_at(self, child_id: str) -> datetime | None: ...
    @abstractmethod
    def set_started_at(self, child_id: str, ts: datetime) -> None: ...
    @abstractmethod
    def clear_started_at(self, child_id: str) -> None: ...

    @abstractmethod
    def get_last_activity(self, child_id: str) -> datetime | None: ...
    @abstractmethod
    def set_last_activity(self, child_id: str, ts: datetime) -> None: ...
    @abstractmethod
    def clear_last_activity(self, child_id: str) -> None: ...

    @abstractmethod
    def is_soft_warned(self, child_id: str) -> bool: ...
    @abstractmethod
    def mark_soft_warned(self, child_id: str) -> None: ...
    @abstractmethod
    def clear_soft_warned(self, child_id: str) -> None: ...

    @abstractmethod
    def get_ended(self, child_id: str) -> str | None: ...
    @abstractmethod
    def set_ended(self, child_id: str, reason: str) -> None: ...
    @abstractmethod
    def clear_ended(self, child_id: str) -> None: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemorySessionStore(SessionStore):
    """Thread-safe dict-backed session state. Default for dev / tests."""

    def __init__(self) -> None:
        self._started: dict[str, datetime] = {}
        self._activity: dict[str, datetime] = {}
        self._soft_warned: set[str] = set()
        self._ended: dict[str, str] = {}
        self._lock = threading.Lock()

    def get_started_at(self, child_id: str) -> datetime | None:
        with self._lock:
            return deepcopy(self._started[child_id]) if child_id in self._started else None

    def set_started_at(self, child_id: str, ts: datetime) -> None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        with self._lock:
            self._started[child_id] = ts

    def clear_started_at(self, child_id: str) -> None:
        with self._lock:
            self._started.pop(child_id, None)

    def get_last_activity(self, child_id: str) -> datetime | None:
        with self._lock:
            return deepcopy(self._activity[child_id]) if child_id in self._activity else None

    def set_last_activity(self, child_id: str, ts: datetime) -> None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        with self._lock:
            self._activity[child_id] = ts

    def clear_last_activity(self, child_id: str) -> None:
        with self._lock:
            self._activity.pop(child_id, None)

    def is_soft_warned(self, child_id: str) -> bool:
        with self._lock:
            return child_id in self._soft_warned

    def mark_soft_warned(self, child_id: str) -> None:
        with self._lock:
            self._soft_warned.add(child_id)

    def clear_soft_warned(self, child_id: str) -> None:
        with self._lock:
            self._soft_warned.discard(child_id)

    def get_ended(self, child_id: str) -> str | None:
        with self._lock:
            return self._ended.get(child_id)

    def set_ended(self, child_id: str, reason: str) -> None:
        with self._lock:
            self._ended[child_id] = reason

    def clear_ended(self, child_id: str) -> None:
        with self._lock:
            self._ended.pop(child_id, None)


# ---------------------------------------------------------------------------
# Redis implementation
# ---------------------------------------------------------------------------


class RedisSessionStore(SessionStore):
    """`SessionStore` backed by `redis.asyncio`.

    Keys are namespaced under `niki:session:{child_id}:{field}`
    with a 24h TTL. Failed operations raise; callers (the
    `FallbackSessionStore` wrapper) decide whether to retry or
    degrade.
    """

    KEY_PREFIX = "niki:session:"

    def __init__(
        self,
        *,
        client: Any = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        key_prefix: str = KEY_PREFIX,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._key_prefix = key_prefix

    async def _redis(self):
        """Lazy client resolution — same pattern as
        `RedisClient.get_client()` in `niki/infrastructure/redis.py`."""
        if self._client is not None:
            return self._client
        from niki.infrastructure.redis import RedisClient

        self._client = await RedisClient.get_client()
        return self._client

    @staticmethod
    def _k(prefix: str, child_id: str, field: str) -> str:
        return f"{prefix}{child_id}:{field}"

    async def _get(self, key: str) -> str | None:
        client = await self._redis()
        return await client.get(key)

    async def _set(self, key: str, value: str) -> None:
        client = await self._redis()
        await client.set(key, value, ex=self._ttl)

    async def _delete(self, *keys: str) -> None:
        client = await self._redis()
        if keys:
            await client.delete(*keys)

    async def get_started_at(self, child_id: str) -> datetime | None:
        v = await self._get(self._k(self._key_prefix, child_id, "started_at"))
        return _parse_iso(v) if v else None

    async def set_started_at(self, child_id: str, ts: datetime) -> None:
        await self._set(
            self._k(self._key_prefix, child_id, "started_at"),
            _iso(ts),
        )

    async def clear_started_at(self, child_id: str) -> None:
        await self._delete(self._k(self._key_prefix, child_id, "started_at"))

    async def get_last_activity(self, child_id: str) -> datetime | None:
        v = await self._get(self._k(self._key_prefix, child_id, "last_activity"))
        return _parse_iso(v) if v else None

    async def set_last_activity(self, child_id: str, ts: datetime) -> None:
        await self._set(
            self._k(self._key_prefix, child_id, "last_activity"),
            _iso(ts),
        )

    async def clear_last_activity(self, child_id: str) -> None:
        await self._delete(self._k(self._key_prefix, child_id, "last_activity"))

    async def is_soft_warned(self, child_id: str) -> bool:
        v = await self._get(self._k(self._key_prefix, child_id, "soft_warned"))
        return v == "1"

    async def mark_soft_warned(self, child_id: str) -> None:
        await self._set(
            self._k(self._key_prefix, child_id, "soft_warned"),
            "1",
        )

    async def clear_soft_warned(self, child_id: str) -> None:
        await self._delete(self._k(self._key_prefix, child_id, "soft_warned"))

    async def get_ended(self, child_id: str) -> str | None:
        return await self._get(self._k(self._key_prefix, child_id, "ended"))

    async def set_ended(self, child_id: str, reason: str) -> None:
        await self._set(self._k(self._key_prefix, child_id, "ended"), reason)

    async def clear_ended(self, child_id: str) -> None:
        await self._delete(self._k(self._key_prefix, child_id, "ended"))


# ---------------------------------------------------------------------------
# Fallback wrapper
# ---------------------------------------------------------------------------


class FallbackSessionStore(SessionStore):
    """Wrap a primary store; on failure, use fallback.

    The async methods on the primary store can raise (Redis
    down / network blip). On the FIRST failure we log a
    warning, flip `_in_fallback = True`, and route the call
    to the in-memory fallback. Subsequent calls stay on
    fallback for the rest of the process lifetime; the
    SessionLimiter is unaffected.
    """

    def __init__(
        self,
        primary: SessionStore,
        fallback: SessionStore,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._in_fallback = False
        self._lock = threading.Lock()

    @property
    def in_fallback(self) -> bool:
        return self._in_fallback

    def _maybe_flip(self, exc: Exception) -> None:
        with self._lock:
            if self._in_fallback:
                return
            logger.warning(
                "session_store.fallback_engaged",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            self._in_fallback = True

    async def _run(self, primary_attr: str, *args: Any) -> Any:
        # Once we've flipped to fallback, never call primary
        # again (slowapi-style). This avoids hammering a dead
        # Redis and getting latency spikes on every request.
        if self._in_fallback:
            return await self._call(self._fallback, primary_attr, *args)
        try:
            return await self._call(self._primary, primary_attr, *args)
        except Exception as exc:
            self._maybe_flip(exc)
            return await self._call(self._fallback, primary_attr, *args)

    @staticmethod
    async def _call(store: SessionStore, attr: str, *args: Any) -> Any:
        result = getattr(store, attr)(*args)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def get_started_at(self, child_id: str) -> datetime | None:
        return await self._run("get_started_at", child_id)

    async def set_started_at(self, child_id: str, ts: datetime) -> None:
        await self._run("set_started_at", child_id, ts)

    async def clear_started_at(self, child_id: str) -> None:
        await self._run("clear_started_at", child_id)

    async def get_last_activity(self, child_id: str) -> datetime | None:
        return await self._run("get_last_activity", child_id)

    async def set_last_activity(self, child_id: str, ts: datetime) -> None:
        await self._run("set_last_activity", child_id, ts)

    async def clear_last_activity(self, child_id: str) -> None:
        await self._run("clear_last_activity", child_id)

    async def is_soft_warned(self, child_id: str) -> bool:
        return await self._run("is_soft_warned", child_id)

    async def mark_soft_warned(self, child_id: str) -> None:
        await self._run("mark_soft_warned", child_id)

    async def clear_soft_warned(self, child_id: str) -> None:
        await self._run("clear_soft_warned", child_id)

    async def get_ended(self, child_id: str) -> str | None:
        return await self._run("get_ended", child_id)

    async def set_ended(self, child_id: str, reason: str) -> None:
        await self._run("set_ended", child_id, reason)

    async def clear_ended(self, child_id: str) -> None:
        await self._run("clear_ended", child_id)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    """Return the process-wide session store.

    Default: in-memory (dev / single-process).

    Production: the SessionStore should be set once at app
    startup via `set_session_store(...)`. We don't auto-build
    a Redis store here because SessionStore methods are async
    on the Redis impl and the in-memory impl is sync — the
    caller decides which to inject.
    """
    global _store
    if _store is None:
        _store = InMemorySessionStore()
    return _store


def set_session_store(store: SessionStore | None) -> None:
    """Override the process-wide session store (tests / Redis swap)."""
    global _store
    _store = store


# ---------------------------------------------------------------------------
# Async <-> sync adapter for SessionLimiter
# ---------------------------------------------------------------------------


class SyncSessionStore(SessionStore):
    """Adapt an async store to the sync SessionStore interface.

    SessionLimiter historically uses sync method calls. The
    Redis impl is async (uses `redis.asyncio`). This adapter
    bridges the two by running each call on the running event
    loop (or creating one if needed).

    Use this when you want the SessionLimiter to share state
    across workers but you don't want to make the limiter's
    internal call sites async.
    """

    def __init__(self, async_store: SessionStore) -> None:
        self._store = async_store

    def _run(self, coro):
        # Try to attach to the running loop first; otherwise
        # spin up a fresh one. `asyncio.run()` would close the
        # existing loop, which we don't want, so use
        # `asyncio.get_event_loop()` only when no loop is
        # running on this thread.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # We're inside an async context — schedule the
            # coroutine and wait for it.
            import concurrent.futures

            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=5)
        # No running loop — safe to spin one up.
        return asyncio.run(coro)

    def get_started_at(self, child_id: str) -> datetime | None:
        return self._run(self._store.get_started_at(child_id))

    def set_started_at(self, child_id: str, ts: datetime) -> None:
        self._run(self._store.set_started_at(child_id, ts))

    def clear_started_at(self, child_id: str) -> None:
        self._run(self._store.clear_started_at(child_id))

    def get_last_activity(self, child_id: str) -> datetime | None:
        return self._run(self._store.get_last_activity(child_id))

    def set_last_activity(self, child_id: str, ts: datetime) -> None:
        self._run(self._store.set_last_activity(child_id, ts))

    def clear_last_activity(self, child_id: str) -> None:
        self._run(self._store.clear_last_activity(child_id))

    def is_soft_warned(self, child_id: str) -> bool:
        return self._run(self._store.is_soft_warned(child_id))

    def mark_soft_warned(self, child_id: str) -> None:
        self._run(self._store.mark_soft_warned(child_id))

    def clear_soft_warned(self, child_id: str) -> None:
        self._run(self._store.clear_soft_warned(child_id))

    def get_ended(self, child_id: str) -> str | None:
        return self._run(self._store.get_ended(child_id))

    def set_ended(self, child_id: str, reason: str) -> None:
        self._run(self._store.set_ended(child_id, reason))

    def clear_ended(self, child_id: str) -> None:
        self._run(self._store.clear_ended(child_id))
