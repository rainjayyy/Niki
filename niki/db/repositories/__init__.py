"""Persistence layer for Niki.

Repositories hide the storage backend from business services. Each
repository ships two implementations:

    - PostgresXxxRepository  — production, talks to PostgreSQL
    - InMemoryXxxRepository  — used by tests and the dev fallback

Business code (niki/auth/lifecycle.py, etc.) depends only on the
abstract base class `XxxRepository`, so swapping backends is a
one-line config change.
"""

from niki.db.repositories.account_repo import (
    AccountRepository,
    InMemoryAccountRepository,
    PostgresAccountRepository,
)
from niki.db.repositories.treehole_repo import (
    InMemoryTreeholeRepository,
    PostgresTreeholeRepository,
    TreeholeRepository,
    get_treehole_repository,
    set_treehole_repository,
)

__all__ = [
    "AccountRepository",
    "InMemoryAccountRepository",
    "PostgresAccountRepository",
    "TreeholeRepository",
    "InMemoryTreeholeRepository",
    "PostgresTreeholeRepository",
    "get_treehole_repository",
    "set_treehole_repository",
]
