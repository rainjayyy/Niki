"""Database package — SQLAlchemy ORM, async sessions, repositories.

BE-STOR-001: replaces in-memory dicts with persistent PostgreSQL storage.

Public API:
    - niki.db.Base: SQLAlchemy DeclarativeBase (re-exported from
      niki.infrastructure.postgres so existing imports keep working).
    - niki.db.get_session: async context manager for AsyncSession.
    - niki.db.models.*: ORM classes (Parent, Child, ...).
    - niki.db.repositories.AccountRepository: persistence facade.

Importing `niki.db.models` registers every model with Base.metadata,
which Alembic needs to autogenerate migrations.
"""

from __future__ import annotations

from niki.db.base import Base
from niki.db.session import get_session, session_scope

__all__ = [
    "Base",
    "get_session",
    "session_scope",
]
