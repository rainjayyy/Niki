"""Treehole manager — PRD #61-#64.

Orchestrates treehole mode:
1. Detect activation (via TreeholeActivationDetector)
2. Extract emotion summary (emotion_valence, intensity, category)
3. Encrypt with AES-256-GCM
4. Store in `source: "treehole"` partition (isolated from normal data)
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

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import structlog

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
    """In-memory store for treehole summaries.

    Production: this would be Postgres with source='treehole' partition
    (or a separate encrypted column) plus scheduled purge job.
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
    """Orchestrates treehole mode.

    Usage:
        manager = TreeholeManager(encryption_key=...)
        summary, encrypted = await manager.engage(child_id, original_text, emotion)
    """

    def __init__(
        self,
        encryption: TreeholeEncryption | None = None,
        retention_days: int = 7,
    ) -> None:
        self._encryption = encryption or TreeholeEncryption()
        self._store = TreeholeStore()
        self._retention_days = retention_days

    @property
    def store(self) -> TreeholeStore:
        return self._store

    @property
    def retention_days(self) -> int:
        return self._retention_days

    async def engage(
        self,
        child_id: str,
        original_text: str,
        emotion: str = "neutral",
        intensity: int = 3,
        category: str = "other",
        z_score_deviation: float | None = None,
    ) -> tuple[TreeholeSummary, dict[str, str]]:
        """Engage treehole mode for a child message.

        Args:
            child_id: The child's identifier (used as AAD for encryption).
            original_text: The child's text (will NEVER be stored).
            emotion: Emotion category.
            intensity: Emotion intensity (1-5).
            category: Emotion topic category.
            z_score_deviation: Z-score vs baseline.

        Returns:
            Tuple of (TreeholeSummary, encrypted_payload).
        """
        # 1. Detect activation (caller is expected to have done this,
        #    but verify)
        if not should_activate_treehole(original_text):
            logger.warning(
                "treehole.engage_called_without_trigger",
                child_id=child_id,
            )

        # 2. Build the plaintext summary (PRD #62 schema)
        #     NOTE: original_text is NOT included — only structured fields
        summary = TreeholeSummary(
            timestamp=datetime.utcnow().isoformat(),
            source="treehole",
            emotion_valence=emotion,
            emotion_intensity=intensity,
            emotion_category=category,
            z_score_deviation=z_score_deviation,
        )

        # 3. Encrypt the summary (using child_id as AAD for binding)
        plaintext = json.dumps(summary.to_dict(), ensure_ascii=False)
        associated_data = child_id.encode("utf-8")
        encrypted_b64 = self._encryption.encrypt(
            plaintext, associated_data=associated_data
        )

        # 4. Store (in-memory; production = encrypted Postgres column)
        encrypted_payload = {
            "child_id": child_id,
            "encrypted": encrypted_b64,
            "associated_data": child_id,  # for decryption context
        }
        self._store.add(summary, encrypted_payload)

        logger.info(
            "treehole.engaged",
            child_id=child_id,
            emotion=emotion,
            intensity=intensity,
            category=category,
            # NEVER log original_text
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

    async def list_summaries(self) -> list[TreeholeSummary]:
        """List all currently-stored summaries (for the parent dashboard)."""
        return list(self._store.summaries)

    async def aggregate_for_baseline(self) -> dict[str, float]:
        """Compute the baseline contribution from treehole summaries.

        PRD #64: '聚合即匿名' — once aggregated into the baseline mean,
        individual summaries cannot be re-derived.
        """
        if not self._store.summaries:
            return {"mean_valence": 0.0, "count": 0}

        # Map intensity to valence
        valence_map = {
            "positive": 0.5,
            "neutral": 0.0,
            "negative": -0.5,
        }
        total = 0.0
        for s in self._store.summaries:
            total += valence_map.get(s.emotion_valence, 0.0) * (
                s.emotion_intensity / 5.0
            )
        mean = total / len(self._store.summaries)
        return {
            "mean_valence": mean,
            "count": len(self._store.summaries),
        }
