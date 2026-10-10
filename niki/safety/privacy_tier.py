"""Four-level privacy tier classification — PRD #61-#64, #29.

Classifies a child message into one of four privacy tiers, matching
the resume's "四级隐私策略（日常→轻度负面→高危告警→树洞私密）":

    DAILY            — routine conversation; parent weekly report shows full text
    MILD_NEGATIVE    — mild negative emotion; report shows trend, not raw text
    HIGH_RISK        — self-harm signal; immediate parent alert + raw text push
    TREEHOLE_PRIVATE — child requested confidentiality; encrypt + aggregate only

The classifier is a pure function (apart from optional treehole
activation logic) so it can be invoked synchronously from the chat
path with <1ms overhead.

Related systems:
- `safety/input_gateway.InputAction` (4 actions): how the system
  *reacts* to incoming text (allow / downgrade / escalate / block).
  PrivacyTier is the orthogonal *classification* of how the data
  flows downstream (visibility, retention, alerting).
- `treehole/`: the actual AES-256-GCM encryption + 7-day TTL for
  TREEHOLE_PRIVATE content. PrivacyTierClassifier just decides WHEN
  to engage treehole.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from niki.safety.dictionaries import SENSITIVE_WORDS
from niki.treehole.activation import should_activate_treehole

logger = structlog.get_logger(__name__)


class PrivacyTier(StrEnum):
    """Four-level privacy tier for a child message.

    Ordered from least to most sensitive. Each tier dictates
    downstream visibility / retention / alerting policies:

        DAILY             → normal logging, full text in weekly report
        MILD_NEGATIVE     → normal logging, trend only (no raw text) in report
        HIGH_RISK         → trigger parent alert immediately (raw text + summary)
        TREEHOLE_PRIVATE  → AES-256-GCM encrypted summary only;
                            parent report sees aggregate trend, never raw text
    """

    DAILY = "daily"
    MILD_NEGATIVE = "mild_negative"
    HIGH_RISK = "high_risk"
    TREEHOLE_PRIVATE = "treehole_private"


# Tier order for severity comparison (higher index = more sensitive).
_TIER_ORDER = {
    PrivacyTier.DAILY: 0,
    PrivacyTier.MILD_NEGATIVE: 1,
    PrivacyTier.HIGH_RISK: 2,
    PrivacyTier.TREEHOLE_PRIVATE: 3,
}


# Words that map a message to MILD_NEGATIVE (mild negative emotion).
# Kept conservative to avoid false positives in 3-12 age range.
_MILD_NEGATIVE_WORDS_CN = {
    "不开心",
    "不高兴",
    "有点难过",
    "有点伤心",
    "心情不好",
    "烦",
    "郁闷",
    "委屈",
    "失望",
}
_MILD_NEGATIVE_WORDS_EN = {
    "sad",
    "upset",
    "unhappy",
    "bored",
    "annoyed",
    "a little sad",
}


@dataclass(frozen=True)
class PrivacyTierVerdict:
    """Result of running the privacy tier classifier."""

    tier: PrivacyTier
    matched_category: str | None = None  # e.g. "self_harm", "treehole_phrase"
    matched_words: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def is_sensitive(self) -> bool:
        """True when tier is anything beyond DAILY."""
        return self.tier != PrivacyTier.DAILY

    @property
    def requires_parent_alert(self) -> bool:
        """True when tier warrants a real-time push to the parent."""
        return self.tier == PrivacyTier.HIGH_RISK

    @property
    def should_encrypt(self) -> bool:
        """True when raw text must not be retained in cleartext."""
        return self.tier == PrivacyTier.TREEHOLE_PRIVATE


class PrivacyTierClassifier:
    """Stateless four-tier privacy classifier.

    Decision order (most-severe wins):

    1. If `should_activate_treehole(text)` is True → TREEHOLE_PRIVATE
    2. If self_harm words matched → HIGH_RISK
    3. If other sensitive category (profanity/violence/sexual/drugs)
       matched → HIGH_RISK (treated as serious, mirrors InputAction
       BLOCK / ESCALATE)
    4. If mild-negative word matched → MILD_NEGATIVE
    5. Else → DAILY

    Usage:
        clf = PrivacyTierClassifier()
        verdict = clf.classify("今天被小明欺负了")
        # verdict.tier == PrivacyTier.MILD_NEGATIVE

        verdict = clf.classify("我想自杀")
        # verdict.tier == PrivacyTier.HIGH_RISK
    """

    # Categories from SENSITIVE_WORDS that map to HIGH_RISK rather
    # than MILD_NEGATIVE. Self-harm is the canonical HIGH_RISK
    # signal (PRD #29); the others are also escalated by policy.
    HIGH_RISK_CATEGORIES = frozenset({"self_harm", "violence", "sexual", "drugs"})

    def __init__(
        self,
        *,
        enable_treehole: bool = True,
    ) -> None:
        """Args:
        enable_treehole: when False, skip step 1 (used in tests
            that want to classify treehole-shaped text as
            HIGH_RISK or MILD_NEGATIVE instead).
        """
        self._enable_treehole = enable_treehole

    def classify(self, text: str) -> PrivacyTierVerdict:
        """Run all privacy classification checks on the text.

        Order matters — most-severe wins, matching the convention
        used by `safety/input_gateway.py`.
        """
        if not text or not text.strip():
            return PrivacyTierVerdict(
                tier=PrivacyTier.DAILY,
                reason="empty input",
            )

        text_clean = text.strip()
        text_lower = text_clean.lower()

        # 1. Treehole activation — child requested confidentiality
        if self._enable_treehole and should_activate_treehole(text_clean):
            matched = _collect_treehole_cues(text_clean, text_lower)
            logger.info(
                "privacy_tier.treehole_classified",
                words=matched,
            )
            return PrivacyTierVerdict(
                tier=PrivacyTier.TREEHOLE_PRIVATE,
                matched_category="treehole_phrase",
                matched_words=matched,
                reason="Child expressed confidentiality — encrypt + aggregate only",
            )

        # 2. Self-harm (and other HIGH_RISK categories from SENSITIVE_WORDS)
        for category in ("self_harm", "violence", "sexual", "drugs"):
            matches = self._scan_category(text_clean, text_lower, category)
            if matches:
                logger.warning(
                    "privacy_tier.high_risk_classified",
                    category=category,
                    words=matches,
                )
                return PrivacyTierVerdict(
                    tier=PrivacyTier.HIGH_RISK,
                    matched_category=category,
                    matched_words=matches,
                    reason=f"High-risk content detected: {category}",
                )

        # 3. Mild negative emotion (separate from SENSITIVE_WORDS)
        mild_matches = _scan_mild_negative(text_clean, text_lower)
        if mild_matches:
            logger.info(
                "privacy_tier.mild_negative_classified",
                words=mild_matches,
            )
            return PrivacyTierVerdict(
                tier=PrivacyTier.MILD_NEGATIVE,
                matched_category="mild_negative",
                matched_words=mild_matches,
                reason="Mild negative emotion detected — trend only, no raw text",
            )

        # 4. Default — daily routine
        return PrivacyTierVerdict(
            tier=PrivacyTier.DAILY,
            reason="no privacy signals detected",
        )

    @staticmethod
    def _scan_category(
        text: str,
        text_lower: str,
        category: str,
    ) -> list[str]:
        """Find all words from SENSITIVE_WORDS[category] in text."""
        words = SENSITIVE_WORDS.get(category, set())
        if not words:
            return []

        found: list[str] = []
        for word in words:
            cand = word if any("一" <= c <= "鿿" for c in word) else word.lower()
            haystack = text if any("一" <= c <= "鿿" for c in cand) else text_lower
            if cand in haystack:
                found.append(word)
        return found

    @staticmethod
    def pick_most_sensitive(
        verdicts: list[PrivacyTierVerdict],
    ) -> PrivacyTierVerdict:
        """Reduce a list of verdicts to the single most-sensitive tier.

        Used when fusing multiple agent outputs (e.g. an encyclopedia
        answer + an emotion empathic reply) and we need the highest
        privacy tier across them.
        """
        if not verdicts:
            return PrivacyTierVerdict(tier=PrivacyTier.DAILY)
        return max(
            verdicts,
            key=lambda v: _TIER_ORDER.get(v.tier, 0),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _scan_mild_negative(text: str, text_lower: str) -> list[str]:
    """Find any mild-negative cue words in text (CN + EN)."""
    found: list[str] = []
    for word in _MILD_NEGATIVE_WORDS_CN:
        if word in text:  # CN: case-insensitive N/A
            found.append(word)
    for word in _MILD_NEGATIVE_WORDS_EN:
        if word.lower() in text_lower:
            found.append(word)
    return found


def _collect_treehole_cues(text: str, text_lower: str) -> list[str]:
    """Best-effort collection of the phrases that triggered treehole.

    Used for logging / audit. Returns up to 3 matched cues.
    """
    cues = [
        phrase
        for phrase in (
            "不想说",
            "别告诉",
            "别跟",
            "秘密",
            "不要告诉",
            "保密",
        )
        if phrase in text
    ]
    if not cues:
        # Fall back to the first 30 chars so the log line is still useful
        cues = [text[:30]]
    return cues[:3]
