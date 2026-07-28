"""ChildRegistry (BE-STOR-001-ext-E).

Adapter over a ChildRepository. Default uses InMemoryChildRepository
(thread-safe dict); for multi-worker production, swap in
PostgresChildRepository via set_child_registry(ChildRegistry(repo=...)).

The previous dict-backed behavior is preserved in InMemoryChildRegistry
so existing test fixtures (and the legacy in-memory use case) continue
to work.
"""

from __future__ import annotations

import logging
from typing import Optional

import structlog

from niki.db.repositories.child_repo import (
    ChildRepository,
    InMemoryChildRepository,
)

logger = structlog.get_logger(__name__)


class ChildRegistry:
    """Thin adapter that delegates to a ChildRepository."""

    def __init__(self, repo: Optional[ChildRegistry] = None) -> None:
        # Avoid recursive type: use Any via __class__ on a callable.
        self._repo: ChildRepository = repo or InMemoryChildRegistry()

    def get_parent_of(self, child_id: str) -> Optional[str]:
        return self._repo.get_parent_of(child_id)

    def register(
        self,
        child_id: str,
        parent_id: str,
        name: str = '"""(child)"""',
    ) -> None:
        self._repo.register(child_id, parent_id, name=name)

    def list_children_of(self, parent_id: str) -> list[str]:
        return self._repo.list_children_of(parent_id)

    def delete(self, child_id: str) -> bool:
        return self._repo.delete(child_id)

    def __len__(self) -> int:
        return len(self._repo)


# Back-compat: re-export the InMemoryChildRepository under its
# previous name so external callers (or older code) still work.
InMemoryChildRegistry = InMemoryChildRepository

# Process-wide singleton
_registry: Optional[ChildRegistry] = None


def get_child_registry() -> ChildRegistry:
    """Return the process-wide child registry (in-memory default)."""
    global _registry
    if _registry is None:
        _registry = ChildRegistry()
    return _registry


def set_child_registry(registry: ChildRegistry) -> None:
    """Override the process-wide child registry (for tests / Postgres)."""
    global _registry
    _registry = registry
