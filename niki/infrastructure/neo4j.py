"""Neo4j graph database driver.

Neo4j is used for the cognitive graph:
- Entity nodes and relationship edges
- Entity alignment (Chroma-backed semantic deduplication)
- Relationship edge time decay (weight × 0.5 at 30d, × 0.1 at 60d)
- Interest graph and learning progress tracking
"""

import time
from typing import Any

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase

from niki.config.settings import get_settings
from niki.utils.exceptions import GraphDatabaseError

logger = structlog.get_logger(__name__)


class Neo4jClient:
    """Singleton Neo4j async driver.

    Usage:
        driver = await Neo4jClient.get_driver()
        async with driver.session() as session:
            result = await session.run("MATCH (n) RETURN n LIMIT 1")
    """

    _driver: AsyncDriver | None = None

    @classmethod
    async def get_driver(cls) -> AsyncDriver:
        """Get or create the Neo4j async driver.

        Returns:
            An initialized neo4j.AsyncDriver.

        Raises:
            GraphDatabaseError: If connection fails.
        """
        if cls._driver is not None:
            return cls._driver

        settings = get_settings()
        logger.info("neo4j.connecting", uri=settings.neo4j_uri)

        try:
            cls._driver = AsyncGraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
                max_connection_lifetime=3600,
                max_connection_pool_size=20,
                connection_acquisition_timeout=10,
            )
            # Verify connectivity
            await cls._driver.verify_connectivity()
            logger.info("neo4j.connected")
        except Exception as exc:
            logger.error("neo4j.connection_failed", error=str(exc))
            raise GraphDatabaseError(f"Neo4j connection failed: {exc}") from exc

        return cls._driver

    @classmethod
    async def health_check(cls) -> dict[str, Any]:
        """Check Neo4j connectivity.

        Returns:
            Dict with 'status' and 'latency_ms' keys.
        """
        try:
            driver = await cls.get_driver()
            start = time.monotonic()
            await driver.verify_connectivity()
            latency = (time.monotonic() - start) * 1000
            return {"status": "healthy", "latency_ms": round(latency, 2)}
        except Exception as exc:
            return {"status": "unhealthy", "latency_ms": None, "error": str(exc)}

    @classmethod
    async def close(cls) -> None:
        """Close the Neo4j driver gracefully."""
        if cls._driver is not None:
            await cls._driver.close()
            cls._driver = None
            logger.info("neo4j.disconnected")
