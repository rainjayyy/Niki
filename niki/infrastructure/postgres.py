"""PostgreSQL connection via SQLAlchemy async engine.

PostgreSQL is used for:
- User accounts (children, parents)
- Conversation logs (transcribed text, metadata)
- Emotion records (per-day aggregates)
- Parent dashboard data (weekly reports, alerts)
- System configuration (topic blacklists, session limits)
"""

import time
from collections.abc import AsyncGenerator
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from niki.config.settings import get_settings
from niki.utils.exceptions import DatabaseConnectionError

logger = structlog.get_logger(__name__)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    pass


class PostgresClient:
    """Singleton PostgreSQL async engine + session factory.

    Usage:
        engine = PostgresClient.get_engine()
        async for session in PostgresClient.get_session():
            # use session
    """

    _engine: AsyncEngine | None = None
    _sessionmaker: async_sessionmaker[AsyncSession] | None = None

    @classmethod
    def get_engine(cls) -> AsyncEngine:
        """Get or create the async SQLAlchemy engine.

        Returns:
            An initialized AsyncEngine.

        Raises:
            DatabaseConnectionError: If engine creation fails.
        """
        if cls._engine is not None:
            return cls._engine

        settings = get_settings()
        logger.info("postgres.connecting", url=_redact_url(settings.database_url))

        try:
            cls._engine = create_async_engine(
                settings.database_url,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                echo=settings.db_echo,
                pool_pre_ping=True,
                pool_recycle=3600,
            )
            logger.info("postgres.engine_created")
        except Exception as exc:
            logger.error("postgres.connection_failed", error=str(exc))
            raise DatabaseConnectionError("PostgreSQL", str(exc)) from exc

        return cls._engine

    @classmethod
    def get_sessionmaker(cls) -> async_sessionmaker[AsyncSession]:
        """Get or create the async session factory."""
        if cls._sessionmaker is None:
            cls._sessionmaker = async_sessionmaker(
                cls.get_engine(),
                class_=AsyncSession,
                expire_on_commit=False,
            )
        return cls._sessionmaker

    @classmethod
    async def get_session(cls) -> AsyncGenerator[AsyncSession, None]:
        """Get a new async database session as a context manager.

        Usage:
            async for session in PostgresClient.get_session():
                result = await session.execute(...)
        """
        sessionmaker = cls.get_sessionmaker()
        async with sessionmaker() as session:
            yield session

    @classmethod
    async def health_check(cls) -> dict[str, Any]:
        """Check PostgreSQL connectivity.

        Returns:
            Dict with 'status' and 'latency_ms' keys.
        """
        try:
            engine = cls.get_engine()
            start = time.monotonic()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            latency = (time.monotonic() - start) * 1000
            return {"status": "healthy", "latency_ms": round(latency, 2)}
        except Exception as exc:
            return {"status": "unhealthy", "latency_ms": None, "error": str(exc)}

    @classmethod
    async def close(cls) -> None:
        """Close the database engine gracefully."""
        if cls._engine is not None:
            await cls._engine.dispose()
            cls._engine = None
            cls._sessionmaker = None
            logger.info("postgres.disconnected")


def _redact_url(url: str) -> str:
    """Redact password from database URL for logging."""
    if "@" in url:
        prefix, suffix = url.split("@", 1)
        if ":" in prefix:
            scheme_user = prefix.rsplit(":", 1)[0]
            return f"{scheme_user}:***@{suffix}"
    return url
