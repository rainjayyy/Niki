"""Treehole purge — PRD #61 (BE-STORG-001-ext-B: via repository).

Auto-purge of treehole records via the active TreeholeRepository:
- Original transcripts deleted after 7 days (PRD: 7-day auto-delete)
- Encrypted summaries also purged (the baseline already aggregated them)
- After purge: only the aggregated baseline mean remains (irreversible)

This is what '聚合即匿名' means: the summary is destroyed, but
its contribution to the daily aggregate lives on (anonymously).
"""

from __future__ import annotations

from datetime import datetime

import structlog

from niki.db.repositories import TreeholeRepository, get_treehole_repository

logger = structlog.get_logger(__name__)


class TreeholePurger:
    """Purges treehole records older than the retention period.

    Should be run by a daily cron job (similar to NightlyPipeline).
    """

    def __init__(
        self,
        retention_days: int = 7,
        repo: TreeholeRepository | None = None,
    ) -> None:
        self.retention_days = retention_days
        self._repo = repo or get_treehole_repository()

    async def purge_expired(self, now: datetime | None = None) -> int:
        """Purge all records whose `expires_at` is in the past.

        Returns the number of records purged.
        """
        return await self._repo.delete_expired(now=now)

    async def purge_all(self) -> int:
        """Force-purge ALL records (for account deletion, PRD #35)."""
        return await self._repo.delete_all()
