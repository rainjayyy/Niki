"""Short-term memory — PRD #46.

Redis-backed cache of the most recent 10 conversation rounds.
Keyed by session_id. Used by all agents for immediate context.

Production: uses Redis (already wired up in P0).
Here: in-memory dict with the same interface, swappable for Redis.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from niki.infrastructure.redis import RedisClient

logger = structlog.get_logger(__name__)


# Max conversation rounds per session (PRD #46)
MAX_ROUNDS = 10


@dataclass
class ConversationTurn:
    """A single turn in a conversation (child or Niki)."""

    role: str  # "child" | "niki"
    text: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    intent: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationTurn:
        ts = data.get("timestamp")
        if isinstance(ts, str):
            data = {**data, "timestamp": datetime.fromisoformat(ts)}
        return cls(**data)


class ShortTermMemory:
    """Recent-N-turns cache per session.

    Backed by Redis in production; the public surface is the same either way.
    """

    def __init__(self, max_rounds: int = MAX_ROUNDS) -> None:
        self.max_rounds = max_rounds
        self._cache: dict[str, list[ConversationTurn]] = {}

    async def get_recent(self, session_id: str) -> list[ConversationTurn]:
        """Return the recent turns for a session (most recent last)."""
        if session_id in self._cache:
            return list(self._cache[session_id])
        # Try Redis
        try:
            redis = await RedisClient.get_client()
            key = RedisClient.make_key("short_term", session_id)
            raw = await redis.get(key)
            if raw:
                data = json.loads(raw)
                turns = [ConversationTurn.from_dict(t) for t in data]
                self._cache[session_id] = turns
                return list(turns)
        except Exception as exc:
            logger.warning(
                "short_term.redis_get_failed",
                error=str(exc),
                fallback="memory",
            )
        return []

    async def append(self, session_id: str, turn: ConversationTurn) -> list[ConversationTurn]:
        """Append a turn; trim to last max_rounds."""
        existing = await self.get_recent(session_id)
        existing.append(turn)
        # Trim
        if len(existing) > self.max_rounds:
            existing = existing[-self.max_rounds :]
        self._cache[session_id] = existing

        # Persist to Redis (best-effort)
        try:
            redis = await RedisClient.get_client()
            key = RedisClient.make_key("short_term", session_id)
            await redis.set(
                key,
                json.dumps([t.to_dict() for t in existing]),
                ex=86400,  # 1 day TTL
            )
        except Exception as exc:
            logger.warning(
                "short_term.redis_set_failed",
                error=str(exc),
                fallback="memory only",
            )

        return existing

    async def clear(self, session_id: str) -> None:
        """Clear a session's memory."""
        self._cache.pop(session_id, None)
        try:
            redis = await RedisClient.get_client()
            key = RedisClient.make_key("short_term", session_id)
            await redis.delete(key)
        except Exception:
            pass

    async def clear_all(self) -> None:
        """Clear all session memory (used for account deletion, PRD #35).

        Clears the in-process cache and scans + deletes every
        `niki:short_term:*` key in Redis. Redis failures are
        logged but do NOT block the local clear — the worst case
        is that a stale Redis entry survives until its 1-day TTL
        expires, which is acceptable for compliance.
        """
        self._cache.clear()
        try:
            redis = await RedisClient.get_client()
            pattern = RedisClient.make_key("short_term", "*")
            deleted = 0
            async for key in redis.scan_iter(match=pattern, count=100):
                await redis.delete(key)
                deleted += 1
            logger.info(
                "short_term.clear_all.redis",
                pattern=pattern,
                deleted=deleted,
            )
        except Exception as exc:
            # Local cache is already cleared; surface the Redis
            # failure as a warning so account deletion does not
            # 500 just because the cache is unreachable.
            logger.warning(
                "short_term.clear_all.redis_failed",
                error=str(exc),
                note="local cache cleared, Redis keys will expire via TTL",
            )


def turns_to_history(
    turns: list[ConversationTurn],
) -> list[dict[str, str]]:
    """Map stored turns to LLM `messages` entries (child -> user, Niki -> assistant)."""
    history: list[dict[str, str]] = []
    for turn in turns:
        role = "user" if turn.role == "child" else "assistant"
        history.append({"role": role, "content": turn.text})
    return history


def format_history(
    history: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Normalise history entries for LLM `messages` payloads."""
    normalized: list[dict[str, str]] = []
    for item in history or []:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            normalized.append({"role": role, "content": content.strip()})
    return normalized


# ---------------------------------------------------------------------------
# Process-wide singleton (shared by the chat orchestrator + SSE endpoint)
# ---------------------------------------------------------------------------


_memory: ShortTermMemory | None = None


def get_short_term_memory() -> ShortTermMemory:
    """Return the process-wide short-term memory (default: Redis + fallback)."""
    global _memory
    if _memory is None:
        _memory = ShortTermMemory()
    return _memory


def set_short_term_memory(memory: ShortTermMemory | None) -> None:
    """Override the process-wide short-term memory (tests / Redis swap)."""
    global _memory
    _memory = memory
