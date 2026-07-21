"""Redis connection pool and client management.

Redis is used for:
- Short-term memory: caching the last 10 conversation rounds
- Session state: active session metadata
- Emotion statistics: 7-day sliding window data (auto-expiring keys)
- Rate limiting: per-user request throttling
"""

import time
from typing import Any

import redis.asyncio as aioredis
import structlog

from niki.config.settings import get_settings
from niki.utils.exceptions import CacheConnectionError

logger = structlog.get_logger(__name__)


class RedisClient:
    """Singleton Redis client with connection pool.

    Usage:
        redis = await RedisClient.get_client()
        await redis.set("niki:key", "value", ex=3600)
    """

    _client: aioredis.Redis | None = None

    @classmethod
    async def get_client(cls) -> aioredis.Redis:
        """Get or create the Redis async client.

        Returns:
            An initialized aioredis.Redis client.

        Raises:
            CacheConnectionError: If connection fails.
        """
        if cls._client is not None:
            return cls._client

        settings = get_settings()
        logger.info("redis.connecting", url=settings.redis_url)

        try:
            cls._client = aioredis.from_url(
                settings.redis_url,
                max_connections=settings.redis_max_connections,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_keepalive=True,
                health_check_interval=30,
            )
            # Verify the connection works
            await cls._client.ping()
            logger.info("redis.connected")
        except Exception as exc:
            logger.error("redis.connection_failed", error=str(exc))
            raise CacheConnectionError(str(exc)) from exc

        return cls._client

    @classmethod
    async def health_check(cls) -> dict[str, Any]:
        """Check Redis connectivity and return health status.

        Returns:
            Dict with 'status' and 'latency_ms' keys.
        """
        try:
            client = await cls.get_client()
            start = time.monotonic()
            await client.ping()
            latency = (time.monotonic() - start) * 1000
            return {"status": "healthy", "latency_ms": round(latency, 2)}
        except Exception as exc:
            return {"status": "unhealthy", "latency_ms": None, "error": str(exc)}

    @classmethod
    async def close(cls) -> None:
        """Close the Redis connection pool gracefully."""
        if cls._client is not None:
            await cls._client.close()
            cls._client = None
            logger.info("redis.disconnected")

    @classmethod
    def make_key(cls, *parts: str) -> str:
        """Build a prefixed Redis key.

        Example:
            RedisClient.make_key("session", "abc123")
            → "niki:session:abc123"
        """
        settings = get_settings()
        return f"{settings.redis_key_prefix}{':'.join(parts)}"
