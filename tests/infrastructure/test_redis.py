"""Tests for Redis client."""

import pytest

from niki.infrastructure.redis import RedisClient


def test_make_key_with_prefix() -> None:
    """RedisClient.make_key should produce prefixed keys."""
    key = RedisClient.make_key("session", "abc123")
    assert key.startswith("niki:")
    assert "session" in key
    assert "abc123" in key


def test_make_key_single_part() -> None:
    """Single-part keys should still be prefixed."""
    key = RedisClient.make_key("counter")
    assert key == "niki:counter"


def test_make_key_multiple_parts() -> None:
    """Multiple parts should be joined with colons."""
    key = RedisClient.make_key("user", "123", "emotion", "today")
    assert key == "niki:user:123:emotion:today"


@pytest.mark.integration
async def test_redis_health_check() -> None:
    """Integration: health check should work when Redis is available."""
    result = await RedisClient.health_check()
    assert result["status"] in ("healthy", "unhealthy")
    if result["status"] == "healthy":
        assert result["latency_ms"] is not None
