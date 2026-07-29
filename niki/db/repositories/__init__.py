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
from niki.db.repositories.auth_audit_repo import (
    AuthAuditEvent,
    AuthAuditLogRepository,
    InMemoryAuthAuditLogRepository,
    PostgresAuthAuditLogRepository,
    get_auth_audit_log_repository,
    set_auth_audit_log_repository,
)
from niki.db.repositories.dashboard_repo import (
    DashboardRepository,
    InMemoryDashboardRepository,
    PostgresDashboardRepository,
    get_dashboard_repository,
    set_dashboard_repository,
)
from niki.db.repositories.emotion_baseline_repo import (
    EmotionBaselineRepository,
    InMemoryEmotionBaselineRepository,
    PostgresEmotionBaselineRepository,
    get_emotion_baseline_repository,
    set_emotion_baseline_repository,
)
from niki.db.repositories.treehole_repo import (
    InMemoryTreeholeRepository,
    PostgresTreeholeRepository,
    TreeholeRepository,
    get_treehole_repository,
    set_treehole_repository,
)
from niki.db.repositories.treehole_aggregation_repo import (
    InMemoryTreeholeAggregationRepository,
    PostgresTreeholeAggregationRepository,
    TreeholeAggregationRepository,
    get_treehole_aggregation_repository,
    set_treehole_aggregation_repository,
)

__all__ = [
    "AccountRepository",
    "InMemoryAccountRepository",
    "PostgresAccountRepository",
    "AuthAuditEvent",
    "AuthAuditLogRepository",
    "InMemoryAuthAuditLogRepository",
    "PostgresAuthAuditLogRepository",
    "get_auth_audit_log_repository",
    "set_auth_audit_log_repository",
    "TreeholeRepository",
    "InMemoryTreeholeRepository",
    "PostgresTreeholeRepository",
    "get_treehole_repository",
    "set_treehole_repository",
    "TreeholeAggregationRepository",
    "InMemoryTreeholeAggregationRepository",
    "PostgresTreeholeAggregationRepository",
    "get_treehole_aggregation_repository",
    "set_treehole_aggregation_repository",
    "DashboardRepository",
    "InMemoryDashboardRepository",
    "PostgresDashboardRepository",
    "get_dashboard_repository",
    "set_dashboard_repository",
    "EmotionBaselineRepository",
    "InMemoryEmotionBaselineRepository",
    "PostgresEmotionBaselineRepository",
    "get_emotion_baseline_repository",
    "set_emotion_baseline_repository",
]

from niki.db.repositories.child_repo import (
    ChildRepository,
    InMemoryChildRepository,
    PostgresChildRepository,
)
