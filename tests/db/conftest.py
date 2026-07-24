"""Shared fixtures for DB tests.

A fresh in-memory SQLite engine per test function; tables created via
`Base.metadata.create_all` so we don't depend on Alembic for unit tests.
Alembic roundtrip is exercised separately in `test_alembic_roundtrip.py`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import niki.db.models  # noqa: F401  -- registers all tables on Base.metadata
from niki.db.base import Base


@pytest_asyncio.fixture
async def async_engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite engine, fresh per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def async_session_factory(async_engine: AsyncEngine):
    """Async session factory bound to the in-memory engine."""
    return async_sessionmaker(async_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Single AsyncSession for the test; auto-rollbacks on exit."""
    async with async_session_factory() as s:
        yield s
