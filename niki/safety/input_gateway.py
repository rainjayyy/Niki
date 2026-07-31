"""Input safety gateway — PRD #39, #40.

The input safety gateway runs BEFORE any LLM call. It performs:
1. Sensitive-word regex filter (profanity, violence, self-harm, sexual, drugs)
2. Topic-blacklist downgrade evaluation (PRD #41)
3. Llama Guard semantic check (optional; falls back to regex-only when disabled)

Returns a verdict describing what should happen next:
- ALLOW — proceed to intent classifier
- DOWNGRADE_TO_EMOTION — route to emotion agent (blacklist or sensitive topic)
- ESCALATE — high-risk content (self-harm) → push alert to parent dashboard
- BLOCK — hard reject (severe content, profanity, sexual)

Design:
- Pure function, stateless (apart from optional Llama Guard client)
- Returns InputVerdict dataclass (not exception) so caller decides
- Async to support future Llama Guard API call
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from niki.metrics import get_registry
from niki.safety.dictionaries import SENSITIVE_WORDS
from niki.safety.topic_blacklist import TopicBlacklist, TopicMatch

logger = structlog.get_logger(__name__)


class InputAction(StrEnum):
    """Action to take based on input safety verdict."""

    ALLOW = "allow"                          # proceed normally
    DOWNGRADE_TO_EMOTION = "downgrade"       # route to emotion agent
    ESCALATE = "escalate"                    # self-harm: parent alert
    BLOCK = "block"                          # hard reject


@dataclass(frozen=True)
class InputVerdict:
    """Result of running the input safety gateway."""

    action: InputAction
    matched_category: str | None = None      # e.g. "self_harm", "topic_blacklist"
    matched_words: list[str] = field(default_factory=list)
    topic_matches: list[TopicMatch] = field(default_factory=list)
    reason: str = ""
    sanitized_text: str = ""                 # cleaned text after rewrites

    @property
    def is_blocked(self) -> bool:
        return self.action == InputAction.BLOCK

    @property
    def is_escalation(self) -> bool:
        return self.action == InputAction.ESCALATE

    @property
    def should_downgrade(self) -> bool:
        return self.action == InputAction.DOWNGRADE_TO_EMOTION


class InputSafetyGateway:
    """Stateless input-side safety evaluator.

    Usage:
        gateway = InputSafetyGateway()
        verdict = await gateway.check("我今天不开心")
        if verdict.is_blocked:
            return error_response(...)
        if verdict.should_downgrade:
            ...
    """

    def __init__(
        self,
        blacklist: TopicBlacklist | None = None,
        enable_llama_guard: bool = False,
    ) -> None:
        self._blacklist = blacklist or TopicBlacklist()
        self._enable_llama_guard = enable_llama_guard

    async def check(self, text: str) -> InputVerdict:
        """Run all input safety checks on the text.

        Order matters — most-severe wins:
        1. Self-harm keywords → ESCALATE
        2. Sexual / violence / drugs / profanity → BLOCK
        3. Topic blacklist match → DOWNGRADE_TO_EMOTION
        4. Otherwise → ALLOW
        """
        if not text or not text.strip():
            return InputVerdict(
                action=InputAction.ALLOW,
                sanitized_text="",
                reason="empty input",
            )

        text_clean = text.strip()

        # 1. Self-harm: highest priority (PRD #29, #64)
        self_harm_matches = self._scan_category(text_clean, "self_harm")
        if self_harm_matches:
            logger.warning(
                "input_safety.self_harm_detected",
                words=self_harm_matches,
            )
            # BE-API-009: count escalations separately from blocks.
            get_registry().SAFETY_BLOCK_TOTAL.labels(
                reason="self_harm",
                severity="escalate",
            ).inc()
            return InputVerdict(
                action=InputAction.ESCALATE,
                matched_category="self_harm",
                matched_words=self_harm_matches,
                reason="Self-harm language detected — push parent alert",
                sanitized_text=text_clean,
            )

        # 2. Other sensitive categories → BLOCK
        # PRD: 5 categories — profanity, violence, sexual, drugs, (self-handled above)
        for category in ("profanity", "violence", "sexual", "drugs"):
            matches = self._scan_category(text_clean, category)
            if matches:
                logger.warning(
                    "input_safety.blocked",
                    category=category,
                    words=matches,
                )
                # BE-API-009: emit a block counter with the matched
                # category as `reason`.
                get_registry().SAFETY_BLOCK_TOTAL.labels(
                    reason=category,
                    severity="block",
                ).inc()
                return InputVerdict(
                    action=InputAction.BLOCK,
                    matched_category=category,
                    matched_words=matches,
                    reason=f"Content violates safety rules: {category}",
                    sanitized_text=text_clean,
                )

        # 3. Topic blacklist → DOWNGRADE_TO_EMOTION
        topic_matches = self._blacklist.match(text_clean)
        if topic_matches:
            logger.info(
                "input_safety.topic_downgrade",
                topics=[m.topic for m in topic_matches],
            )
            return InputVerdict(
                action=InputAction.DOWNGRADE_TO_EMOTION,
                matched_category="topic_blacklist",
                topic_matches=topic_matches,
                reason="Topic in blacklist — route to emotion agent for empathic response",
                sanitized_text=text_clean,
            )

        # 4. Optional Llama Guard semantic check
        if self._enable_llama_guard:
            llama_verdict = await self._llama_guard_check(text_clean)
            if llama_verdict is not None and llama_verdict.action != InputAction.ALLOW:
                return llama_verdict

        # 5. Clean allow
        return InputVerdict(
            action=InputAction.ALLOW,
            sanitized_text=text_clean,
            reason="no safety issues detected",
        )

    @staticmethod
    def _scan_category(text: str, category: str) -> list[str]:
        """Find all sensitive words from a given category that appear in text.

        Uses case-insensitive matching for English keywords.
        """
        words = SENSITIVE_WORDS.get(category, set())
        if not words:
            return []

        text_lower = text.lower()
        found: list[str] = []
        for word in words:
            cand = word if any("一" <= c <= "鿿" for c in word) else word.lower()
            haystack = text if any("一" <= c <= "鿿" for c in cand) else text_lower
            if cand in haystack:
                found.append(word)
        return found

    async def _llama_guard_check(self, text: str) -> InputVerdict | None:
        """Optional Llama Guard semantic check (placeholder).

        Returns None when Llama Guard is not configured. When enabled,
        would call a Llama Guard inference endpoint to catch
        context-dependent violations that regex misses.
        """
        # TODO: integrate with Llama Guard API when API key is configured
        return None
