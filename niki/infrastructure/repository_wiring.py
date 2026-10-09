"""Central Postgres repository wiring.

Every repository that ships a Postgres implementation is switched to a
session-per-call proxy here, so a restart never loses data and a single
long-lived AsyncSession is not shared across concurrent requests.
"""

from __future__ import annotations

from typing import Any

from niki.infrastructure.postgres import PostgresClient


class SessionPerCallRepository:
    """Duck-typed repo proxy that creates a fresh session per call."""

    def __init__(self, repo_class: type) -> None:
        self._repo_class = repo_class

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            async for session in PostgresClient.get_session():
                repo = self._repo_class(session)
                result = await getattr(repo, name)(*args, **kwargs)
                await session.commit()
                return result

        return call


def create_postgres_account_repo() -> SessionPerCallRepository:
    from niki.db.repositories.account_repo import PostgresAccountRepository

    return SessionPerCallRepository(PostgresAccountRepository)


def wire_postgres_repositories() -> None:
    """Replace process-wide in-memory repository singletons with Postgres."""
    from niki.db.repositories import (
        set_audit_log_repository,
        set_auth_audit_log_repository,
        set_dashboard_repository,
        set_emotion_baseline_repository,
        set_letter_repository,
        set_phone_change_repository,
        set_push_device_repository,
        set_push_prefs_repository,
        set_treehole_aggregation_repository,
        set_treehole_repository,
        set_two_factor_repository,
        set_webhook_repository,
    )
    from niki.db.repositories.audit_log_repo import PostgresAuditLogRepository
    from niki.db.repositories.auth_audit_repo import PostgresAuthAuditLogRepository
    from niki.db.repositories.dashboard_repo import PostgresDashboardRepository
    from niki.db.repositories.emotion_baseline_repo import (
        PostgresEmotionBaselineRepository,
    )
    from niki.db.repositories.letter_repo import PostgresLetterRepository
    from niki.db.repositories.phone_change_repo import PostgresPhoneChangeRepository
    from niki.db.repositories.push_device_repo import (
        PostgresPushDeviceRepository,
        PostgresPushPrefsRepository,
    )
    from niki.db.repositories.treehole_aggregation_repo import (
        PostgresTreeholeAggregationRepository,
    )
    from niki.db.repositories.treehole_repo import PostgresTreeholeRepository
    from niki.db.repositories.two_factor_repo import PostgresTwoFactorRepository
    from niki.db.repositories.webhook_repo import PostgresWebhookRepository

    set_audit_log_repository(SessionPerCallRepository(PostgresAuditLogRepository))
    set_auth_audit_log_repository(SessionPerCallRepository(PostgresAuthAuditLogRepository))
    set_dashboard_repository(SessionPerCallRepository(PostgresDashboardRepository))
    set_emotion_baseline_repository(SessionPerCallRepository(PostgresEmotionBaselineRepository))
    set_letter_repository(SessionPerCallRepository(PostgresLetterRepository))
    set_phone_change_repository(SessionPerCallRepository(PostgresPhoneChangeRepository))
    set_push_device_repository(SessionPerCallRepository(PostgresPushDeviceRepository))
    set_push_prefs_repository(SessionPerCallRepository(PostgresPushPrefsRepository))
    set_treehole_aggregation_repository(
        SessionPerCallRepository(PostgresTreeholeAggregationRepository)
    )
    set_treehole_repository(SessionPerCallRepository(PostgresTreeholeRepository))
    set_two_factor_repository(SessionPerCallRepository(PostgresTwoFactorRepository))
    set_webhook_repository(SessionPerCallRepository(PostgresWebhookRepository))
