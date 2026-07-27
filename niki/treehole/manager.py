"""Treehole manager — PRD #61-#64.

Orchestrates treehole mode:
1. Detect activation (via TreeholeActivationDetector)
2. Extract emotion summary (emotion_valence, intensity, category)
3. Encrypt with AES-256-GCM
4. Persist via TreeholeRepository (BE-STORG-001-ext-B) — defaults
   to in-memory storage for single-process dev / tests; Postgres
   recommended for production / multi-worker.
5. Schedule 7-day auto-purge

CRITICAL: treehole ORIGINAL text must NEVER be written to:
- Chroma (long-term vectors)
- Neo4j (cognitive graph)
- Conversation logs
- Daytime memory hook

Only the encrypted EMOTION SUMMARY enters the baseline. After aggregation,
the individual summaries cannot be re-derived (PRD #64: 聚合即匿名).
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from niki.db.repositories import TreeholeRepository, get_treehole_repository
from niki.treehole.activation import should_activate_treehole
from niki.treehole.encryption import TreeholeEncryption

logger = structlog.get_logger(__name__)


@dataclass
class TreeholeSummary:
    """The plaintext emotion summary structure (PRD #62 schema).

    Fields:
        emotion_valence: "negative" | "positive" | "neutral"
        emotion_intensity: 1-5
        emotion_category: "family" | "school" | "self" | "other"
        z_score_deviation: Z-score vs baseline (None if in calibration)
    """

    timestamp: str
    source: str = "treehole"          # always "treehole" for isolation
    emotion_valence: str = "neutral"
    emotion_intensity: int = 3
    emotion_category: str = "other"
    z_score_deviation: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TreeholeSummary:
        return cls(**data)


@dataclass
class TreeholeStore:
    """Legacy in-memory store shape (kept for backward compat).

    Production: persistence goes through `TreeholeManager`'s
    injected `TreeholeRepository`. This class is preserved for any
    caller that still imports the type.
    """

    summaries: list[TreeholeSummary] = field(default_factory=list)
    encrypted_payloads: list[dict[str, str]] = field(default_factory=list)
    pending_purge: list[str] = field(default_factory=list)  # timestamps to purge

    def add(
        self,
        summary: TreeholeSummary,
        encrypted_payload: dict[str, str],
    ) -> None:
        """Add a treehole summary + its encrypted payload."""
        self.summaries.append(summary)
        self.encrypted_payloads.append(encrypted_payload)
        # Schedule purge at 7 days
        self.pending_purge.append(summary.timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_count": len(self.summaries),
            "encrypted_count": len(self.encrypted_payloads),
            "pending_purge_count": len(self.pending_purge),
        }


class TreeholeManager:
    """Orchestrates treehole mode (BE-STORG-001-ext-B: persistence).

    Usage:
        manager = TreeholeManager()                         # in-memory
        manager = TreeholeManager(repo=PostgresTreeholeRepo)  # production
        summary, encrypted = await manager.engage(child_id, ...)
    """

    def __init__(
        self,
        encryption: TreeholeEncryption | None = None,
        retention_days: int = 7,
        repo: TreeholeRepository | None = None,
    ) -> None:
        self._encryption = encryption or TreeholeEncryption()
        self._retention_days = retention_days
        self._repo = repo or get_treehole_repository()

    @property
    def store(self) -> TreeholeStore:
        """Legacy accessor — always returns an empty in-memory store.

        The real state lives in the repository. This property is
        preserved for any caller that introspected the old shape.
        """
        return TreeholeStore()

    @property
    def retention_days(self) -> int:
        return self._retention_days

    @property
    def repo(self) -> TreeholeRepository:
        return self._repo

    async def engage(
        self,
        child_id: str,
        parent_id: str,
        original_text: str,
        emotion: str = "neutral",
        intensity: int = 3,
        category: str = "other",
        z_score_deviation: float | None = None,
    ) -> tuple[TreeholeSummary, dict[str, str]]:
        """Engage treehole mode for a child message.

        Args:
            child_id: The child's identifier (used as AAD for encryption).
            parent_id: The parent's id (for ownership scoping).
            original_text: The child's text (will NEVER be stored).
            emotion: Emotion category.
            intensity: Emotion intensity (1-5).
            category: Emotion topic category.
            z_score_deviation: Z-score vs baseline.

        Returns:
            Tuple of (TreeholeSummary, encrypted_payload_dict).
        """
        if not should_activate_treehole(original_text):
            logger.warning(
                "treehole.engage_called_without_trigger",
                child_id=child_id,
            )

        summary = TreeholeSummary(
            timestamp=datetime.utcnow().isoformat(),
            source="treehole",
            emotion_valence=emotion,
            emotion_intensity=intensity,
            emotion_category=category,
            z_score_deviation=z_score_deviation,
        )

        plaintext = json.dumps(summary.to_dict(), ensure_ascii=False)
        associated_data = child_id.encode("utf-8")
        encrypted_b64 = self._encryption.encrypt(
            plaintext, associated_data=associated_data
        )

        expires_at = datetime.utcnow() + timedelta(days=self._retention_days)
        encrypted_bytes = base64.b64decode(encrypted_b64)
        row_id = await self._repo.add(
            child_id=child_id,
            parent_id=parent_id,
            encrypted_payload=encrypted_bytes,
            associated_data=associated_data,
            expires_at=expires_at,
        )

        encrypted_payload = {
            "id": row_id,
            "child_id": child_id,
            "encrypted": encrypted_b64,
            "associated_data": child_id,  # for decryption context
        }

        logger.info(
            "treehole.engaged",
            row_id=row_id,
            child_id=child_id,
            emotion=emotion,
            intensity=intensity,
            category=category,
        )

        return summary, encrypted_payload

    async def decrypt_summary(
        self,
        encrypted_payload: dict[str, str],
    ) -> TreeholeSummary:
        """Decrypt a stored treehole summary (for baseline computation)."""
        plaintext = self._encryption.decrypt(
            encrypted_payload["encrypted"],
            associated_data=encrypted_payload["child_id"].encode("utf-8"),
        )
        return TreeholeSummary.from_dict(json.loads(plaintext))

    async def list_summaries(
        self, child_id: str | None = None
    ) -> list[TreeholeSummary]:
        """List summaries for a child (or all children if child_id is None).

        Decrypts on the fly. For dashboard reads only — never
        decodes for the engage() / baseline() path (those use
        aggregated forms).
        """
        if child_id is None:
            rows = await self._repo.list_all()
        else:
            rows = await self._repo.list_for_child(child_id)
        out: list[TreeholeSummary] = []
        for row in rows:
            try:
                aad = row["associated_data"] or row["child_id"].encode("utf-8")
                ct = base64.b64encode(row["encrypted_payload"]).decode("ascii")
                plaintext = self._encryption.decrypt(ct, associated_data=aad)
                out.append(
                    TreeholeSummary.from_dict(json.loads(plaintext))
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "treehole.decrypt_failed",
                    row_id=row.get("id"),
                    error=str(exc),
                )
        return out

    async def aggregate_for_baseline(
        self, child_id: str | None = None
    ) -> dict[str, float]:
        """Compute the baseline contribution from treehole summaries.

        PRD #64: '聚合即匿名' — once aggregated into the baseline
        mean, individual summaries cannot be re-derived.
        """
        summaries = await self.list_summaries(child_id=child_id)
        if not summaries:
            return {"mean_valence": 0.0, "count": 0}

        valence_map = {
            "positive": 0.5,
            "neutral": 0.0,
            "negative": -0.5,
        }
        total = 0.0
        for s in summaries:
            total += valence_map.get(s.emotion_valence, 0.0) * (
                s.emotion_intensity / 5.0
            )
        mean = total / len(summaries)
        return {
            "mean_valence": mean,
            "count": len(summaries),
        }

    async def purge_expired(self) -> int:
        """Purge all expired engagements (called by daily cron)."""
        return await self._repo.delete_expired()

    async def purge_all_for_child(self, child_id: str) -> int:
        """Drop every engagement for a child (PRD #35)."""
        return await self._repo.delete_all_for_child(child_id)
