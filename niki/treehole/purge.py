"""Treehole purge — PRD #61.

Auto-purge of treehole records:
- Original transcripts deleted after 7 days (PRD: 7-day auto-delete)
- Encrypted summaries also purged (the baseline already aggregated them)
- After purge: only the aggregated baseline mean remains (irreversible)

This is what '聚合即匿名' means: the summary is destroyed, but
its contribution to the daily aggregate lives on (anonymously).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from niki.treehole.manager import TreeholeStore

logger = structlog.get_logger(__name__)


class TreeholePurger:
    """Purges treehole records older than the retention period.

    Should be run by a daily cron job (similar to NightlyPipeline).
    """

    def __init__(self, retention_days: int = 7) -> None:
        self.retention_days = retention_days

    async def purge_expired(
        self,
        store: TreeholeStore,
        now: datetime | None = None,
    ) -> int:
        """Purge all records older than retention_days.

        Args:
            store: The treehole store to purge from.
            now: Override the current time (for testing).

        Returns:
            Number of records purged.
        """
        now = now or datetime.utcnow()
        cutoff = now - timedelta(days=self.retention_days)

        # Find expired records (ISS-03: capture INDEX, not just timestamp,
        # so we can synchronously drop the parallel encrypted_payload entry)
        expired_indices: list[int] = []
        for idx, summary in enumerate(store.summaries):
            ts = datetime.fromisoformat(summary.timestamp)
            if ts < cutoff:
                expired_indices.append(idx)

        if not expired_indices:
            return 0

        expired_set = set(expired_indices)
        # Purge from all 3 lists (ISS-03: use index-based removal so
        # encrypted_payloads and summaries stay aligned).
        store.summaries[:] = [
            s for i, s in enumerate(store.summaries) if i not in expired_set
        ]
        store.encrypted_payloads[:] = [
            ep
            for i, ep in enumerate(store.encrypted_payloads)
            if i not in expired_set
        ]
        store.pending_purge[:] = [
            ts
            for i, ts in enumerate(store.pending_purge)
            if i not in expired_set
        ]

        logger.info(
            "treehole.purged_expired",
            count=len(expired_indices),
            cutoff=cutoff.isoformat(),
        )
        return len(expired_indices)

    async def purge_all(
        self,
        store: TreeholeStore,
    ) -> int:
        """Force-purge ALL records (for account deletion, PRD #35)."""
        count = len(store.summaries)
        store.summaries.clear()
        store.encrypted_payloads.clear()
        store.pending_purge.clear()
        logger.warning("treehole.purged_all", count=count)
        return count
