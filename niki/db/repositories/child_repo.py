"""ChildRepository abstraction (BE-STOR-001-ext-E).

Replaces the in-memory dict backing of ChildRegistry with a
pluggable repository so the lateral authorization guard
(BE-AUTH-001-rollout-ext) works across worker processes.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from niki.db.models import Child

logger = structlog.getLogger(__name__)


class ChildRepository(ABC):
    """Contract for child registration and parent-of lookup."""

    @abstractmethod
    def get_parent_of(self, child_id):
        return None

    @abstractmethod
    def register(self, child_id, parent_id, name=chr(34) + chr(34) + chr(34) + '(child)' + chr(34) * 3):
        pass

    @abstractmethod
    def list_children_of(self, parent_id):
        return []

    @abstractmethod
    def delete(self, child_id):
        return False


class InMemoryChildRepository(ChildRepository):
    def __init__(self):
        self._children = {}
        self._lock = threading.Lock()

    def get_parent_of(self, child_id):
        return self._children.get(child_id)

    def register(self, child_id, parent_id, name=chr(34) * 3 + '(child)' + chr(34) * 3):
        with self._lock:
            self._children[child_id] = parent_id

    def list_children_of(self, parent_id):
        return [c for c, p in self._children.items() if p == parent_id]

    def delete(self, child_id):
        with self._lock:
            return self._children.pop(child_id, None) is not None

    def __len__(self):
        return len(self._children)


class PostgresChildRepository(ChildRepository):
    def __init__(self, session):
        self._session = session

    def get_parent_of(self, child_id):
        row = self._session.execute(
            select(Child).where(Child.id == child_id)
        ).scalar_one_or_none()
        return row.parent_id if row else None

    def register(self, child_id, parent_id, name=chr(34) * 3 + '(child)' + chr(34) * 3):
        existing = self._session.execute(
            select(Child).where(Child.id == child_id)
        ).scalar_one_or_none()
        if existing is not None:
            existing.parent_id = parent_id
            existing.name = name
        else:
            self._session.add(
                Child(id=child_id, parent_id=parent_id, name=name)
            )
        self._session.flush()

    def list_children_of(self, parent_id):
        rows = self._session.execute(
            select(Child.id).where(Child.parent_id == parent_id)
        ).scalars().all()
        return list(rows)

    def delete(self, child_id):
        row = self._session.execute(
            select(Child).where(Child.id == child_id)
        ).scalar_one_or_none()
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True
