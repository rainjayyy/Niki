"""Alembic migration environment — async-aware.

Reads the connection URL from Niki's `Settings` so the same `.env`
controls both the application and the migrations.

Targets `Base.metadata` (which includes every model under `niki.db.models.*`),
so `alembic revision --autogenerate` picks up schema changes automatically.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import settings + Base so the target metadata is populated.
from niki.config.settings import get_settings
from niki.db.base import Base
import niki.db.models  # noqa: F401 — registers all models on Base.metadata.

config = context.config

# Only configure logging if the ini defines a [formatters] section.
# Otherwise fileConfig() raises KeyError('formatters'); we stay quiet
# (Niki configures structlog itself at app startup).
if config.config_file_name is not None:
    cfg_path = Path(config.config_file_name)
    if cfg_path.is_file():
        text = cfg_path.read_text(encoding="utf-8")
        if "[formatters]" in text or "[loggers]" in text:
            fileConfig(config.config_file_name)

# Override sqlalchemy.url from Niki's settings.
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.database_url)

# Metadata for autogenerate.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL to stdout/file."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Forward migrations onto an open sync connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
