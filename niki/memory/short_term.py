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

    role: str                       # "child" | "niki"
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
        """Clear all session memory (used for account deletion, PRD #35)."""
        self._cache.clear()
        # In production: scan + delete Redis keys with prefix
