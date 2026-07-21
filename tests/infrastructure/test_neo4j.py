"""Tests for Neo4j client."""

import pytest

from niki.infrastructure.neo4j import Neo4jClient


@pytest.mark.integration
async def test_neo4j_health_check() -> None:
    """Integration: health check should work when Neo4j is available."""
    result = await Neo4jClient.health_check()
    assert result["status"] in ("healthy", "unhealthy")
    if result["status"] == "healthy":
        assert result["latency_ms"] is not None
