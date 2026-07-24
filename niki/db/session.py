"""Async session helpers for the application layer.

Two styles are supported:

1. `get_session()` — async generator yielding a session (for FastAPI
   dependencies and repository internals).
2. `session_scope()` — async context manager that auto-commits on
   success and rolls back on exception (for scripts and tests).

Both pull the session from the singleton `PostgresClient.sessionmaker`,
which is initialised lazily by the FastAPI lifespan.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from niki.infrastructure.postgres import PostgresClient


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a fresh AsyncSession for one unit of work.

    The caller is responsible for committing. Pair with FastAPI Depends:
        async def endpoint(session: AsyncSession = Depends(get_session)):
            ...
    """
    async for session in PostgresClient.get_session():
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that commits on success / rolls back on error.

    Usage:
        async with session_scope() as session:
            parent = Parent(...)
            session.add(parent)
        # auto-committed here
    """
    async for session in PostgresClient.get_session():
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
