"""Treehole purge — PRD #61 (BE-STORG-001-ext-B: via repository).

Auto-purge of treehole records via the active TreeholeRepository:
- Original transcripts deleted after 7 days (PRD: 7-day auto-delete)
- Encrypted summaries also purged (the baseline already aggregated them)
- After purge: only the aggregated baseline mean remains (irreversible)

This is what '聚合即匿名' means: the summary is destroyed, but
its contribution to the daily aggregate lives on (anonymously).

BE-TREEHOLE-AGG: `purge_all()` ALSO drops the rolling aggregate
table — that path is destructive (account deletion / test reset)
and the rollup no longer makes sense without underlying data.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from niki.db.repositories import (
    TreeholeAggregationRepository,
    TreeholeRepository,
    get_treehole_aggregation_repository,
    get_treehole_repository,
)

logger = structlog.get_logger(__name__)


class TreeholePurger:
    """Purges treehole records older than the retention period.

    Should be run by a daily cron job (similar to NightlyPipeline).
    """

    def __init__(
        self,
        retention_days: int = 7,
        repo: TreeholeRepository | None = None,
        agg_repo: TreeholeAggregationRepository | None = None,
    ) -> None:
        self.retention_days = retention_days
        # NOTE: explicit `is not None` — an empty in-memory repo
        # is falsy (via __len__) so `or` would silently drop an
        # injected repo.
        self._repo = repo if repo is not None else get_treehole_repository()
        self._agg_repo = agg_repo if agg_repo is not None else get_treehole_aggregation_repository()

    async def purge_expired(self, now: datetime | None = None) -> int:
        """Purge all records whose `expires_at` is in the past.

        Returns the number of records purged. Does NOT touch the
        aggregate table — per-engagement expiry doesn't invalidate
        the irreversible rollup (PRD #64).
        """
        return await self._repo.delete_expired(now=now)

    async def purge_all(self) -> int:
        """Force-purge ALL records (for account deletion, PRD #35).

        Also drops every row in the aggregate table — this path
        is destructive (account deletion / test reset) and the
        rollup no longer makes sense without the underlying data.
        """
        dropped = await self._repo.delete_all()
        # Drop aggregate rows for every child that had one.
        for row in await self._agg_repo.list_all():
            await self._agg_repo.drop_all_for_child(row["child_id"])
        return dropped
