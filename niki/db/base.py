"""Re-export the SQLAlchemy declarative Base from the infrastructure layer.

Keeping this thin indirection lets `niki.infrastructure.postgres.Base`
remain the single source of truth (it owns the engine), while letting
ORM models live under the cleaner `niki.db.models.*` namespace.
"""

from __future__ import annotations

from niki.infrastructure.postgres import Base

__all__ = ["Base"]
