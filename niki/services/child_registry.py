"""ChildRegistry (BE-AUTH-001-rollout-ext)."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


class ChildRegistry(ABC):
    """Contract for parent-of-child lookup."""

    @abstractmethod
    def get_parent_of(self, child_id: str) -> Optional[str]:
        """Return parent_id for this child, or None if not registered."""

    @abstractmethod
    def register(self, child_id: str, parent_id: str) -> None:
        """Register a new (child_id, parent_id) pair. Idempotent."""


class InMemoryChildRegistry(ChildRegistry):
    """Dict-backed registry, thread-safe."""

    def __init__(self) -> None:
        self._children: dict[str, str] = {}
        self._lock = threading.Lock()

    def get_parent_of(self, child_id: str) -> Optional[str]:
        return self._children.get(child_id)

    def register(self, child_id: str, parent_id: str) -> None:
        with self._lock:
            self._children[child_id] = parent_id

    def unregister(self, child_id: str) -> None:
        with self._lock:
            self._children.pop(child_id, None)

    def __len__(self) -> int:
        return len(self._children)


_registry: ChildRegistry | None = None


def get_child_registry() -> ChildRegistry:
    """Return the process-wide child registry (in-memory default)."""
    global _registry
    if _registry is None:
        _registry = InMemoryChildRegistry()
    return _registry


def set_child_registry(registry: ChildRegistry) -> None:
    """Override the process-wide child registry (for tests / Postgres)."""
    global _registry
    _registry = registry
