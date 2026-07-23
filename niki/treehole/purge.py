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

        # Find expired records
        to_purge: list[str] = []  # timestamps
        for summary in store.summaries:
            ts = datetime.fromisoformat(summary.timestamp)
            if ts < cutoff:
                to_purge.append(summary.timestamp)

        if not to_purge:
            return 0

        # Purge from all 3 lists
        to_purge_set = set(to_purge)
        store.summaries[:] = [
            s for s in store.summaries if s.timestamp not in to_purge_set
        ]
        # Note: encrypted_payloads and pending_purge use parallel indices
        # Filter them too
        store.encrypted_payloads[:] = [
            ep for ep in store.encrypted_payloads
            if not any(
                ts in ep.get("encrypted", "") or ts in str(ep)
                for ts in to_purge
            )
        ]
        store.pending_purge[:] = [
            ts for ts in store.pending_purge if ts not in to_purge_set
        ]

        logger.info(
            "treehole.purged_expired",
            count=len(to_purge),
            cutoff=cutoff.isoformat(),
        )
        return len(to_purge)

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
