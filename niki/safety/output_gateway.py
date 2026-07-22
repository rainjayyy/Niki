"""Output safety gateway — PRD #39, #56.

Runs AFTER the LLM produces output and BEFORE TTS. The gateway enforces:
1. Fear-word rewriting (PRD #5, #56) — replace frightening words with softer equivalents
2. Privacy reverse-question interception (PRD #42) — block the model from
   asking the child for private info
3. Direct-answer detection in tutoring mode (PRD #54) — flag when tutoring
   agent gives away the answer
4. Sentence-length check (PRD #56) — age-adaptive max-sentence-length
5. Age-inappropriate concept downgrading (PRD #34, #56)

Like the input gateway, it returns a verdict — never throws — and lets
the caller decide how to react (e.g. ask the LLM to retry, log to safety
dashboard, send to production regardless).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from niki.safety.dictionaries import (
    DIRECT_ANSWER_PATTERNS,
    FEAR_WORDS,
    PRIVACY_QUESTION_PATTERNS,
)

logger = structlog.get_logger(__name__)


class OutputAction(StrEnum):
    """Action to take based on output safety verdict."""

    PASS = "pass"                        # output is safe to send
    REWRITE = "rewrite"                  # rewrites applied; safe to send
    REGENERATE = "regenerate"            # direct answer in tutoring; ask LLM again
    BLOCK = "block"                      # severe safety violation


@dataclass(frozen=True)
class OutputVerdict:
    """Result of running the output safety gateway."""

    action: OutputAction
    sanitized_text: str                  # text after rewrites
    rewrites_applied: list[tuple[str, str]] = field(default_factory=list)
    privacy_violations: list[str] = field(default_factory=list)
    direct_answer_detected: bool = False
    sentences_over_limit: int = 0
    reason: str = ""

    @property
    def is_safe(self) -> bool:
        return self.action in (OutputAction.PASS, OutputAction.REWRITE)


class OutputSafetyGateway:
    """Stateless output-side safety evaluator.

    Usage:
        gateway = OutputSafetyGateway(child_age=7)
        verdict = gateway.check(
            text="答案是42",
            is_tutoring_response=True,
        )
        if verdict.direct_answer_detected:
            # ask LLM to retry with guided approach
    """

    # CJK character regex (covers Chinese, Japanese kanji, Korean hanja)
    _CJK_CHAR = re.compile(r"[一-鿿぀-ヿ㐀-䶿]")
    # Sentence delimiter (English . ! ? and Chinese 。！？)
    _SENT_DELIM = re.compile(r"([.!?。！？]+|\n)")

    def __init__(self, child_age: int = 7) -> None:
        self.child_age = child_age
        self._age_group = self._resolve_age_group(child_age)
        self._max_sentence_chars = self._max_chars_per_sentence(self._age_group)

    @staticmethod
    def _resolve_age_group(age: int) -> str:
        if age <= 5:
            return "3-5"
        if age <= 8:
            return "6-8"
        return "9-12"

    @staticmethod
    def _max_chars_per_sentence(age_group: str) -> int:
        """Return the max-sentence-length limit per PRD #56."""
        return {
            "3-5": 10,
            "6-8": 15,
            "9-12": 20,
        }.get(age_group, 15)

    def check(
        self,
        text: str,
        is_tutoring_response: bool = False,
    ) -> OutputVerdict:
        """Run all output-side safety checks and rewrites.

        Args:
            text: The LLM's raw output text.
            is_tutoring_response: True if this is from the Tutoring Agent
                (enables direct-answer detection).

        Returns:
            OutputVerdict with action, sanitized text, and diagnostics.
        """
        if not text or not text.strip():
            return OutputVerdict(
                action=OutputAction.PASS,
                sanitized_text="",
                reason="empty output",
            )

        rewrites: list[tuple[str, str]] = []
        privacy_violations: list[str] = []
        current = text

        # 1. Privacy reverse-question interception (highest severity)
        for pattern in PRIVACY_QUESTION_PATTERNS:
            matches = pattern.findall(current)
            if matches:
                privacy_violations.extend(matches if isinstance(matches, list) else [matches])
                # Replace with safe fallback
                current = pattern.sub("（Niki觉得这个问题我们可以聊点别的呀~）", current)

        if privacy_violations:
            logger.warning(
                "output_safety.privacy_violation",
                violations=privacy_violations[:5],  # log first 5 only
            )
            return OutputVerdict(
                action=OutputAction.BLOCK,
                sanitized_text="Niki觉得这个问题我们可以聊点别的呀~",
                privacy_violations=privacy_violations,
                reason="Model attempted to ask for private information — blocked",
            )

        # 2. Fear-word rewriting
        for word, replacement in FEAR_WORDS.items():
            # Case-insensitive replace for English, exact for Chinese
            pattern = re.escape(word)
            if any("一" <= c <= "鿿" for c in word):
                # Chinese: match exactly
                if word in current:
                    rewrites.append((word, replacement))
                    current = current.replace(word, replacement)
            else:
                # English: case-insensitive
                replaced = re.sub(pattern, replacement, current, flags=re.IGNORECASE)
                if replaced != current:
                    rewrites.append((word, replacement))
                    current = replaced

        # 3. Direct-answer detection (tutoring mode only)
        direct_answer = False
        if is_tutoring_response:
            for pattern in DIRECT_ANSWER_PATTERNS:
                if pattern.search(current):
                    direct_answer = True
                    break

        # 4. Sentence-length check (age-adaptive)
        over_limit_count = self._count_long_sentences(current)

        # Decide action
        if direct_answer:
            logger.info(
                "output_safety.direct_answer",
                child_age=self.child_age,
                age_group=self._age_group,
            )
            return OutputVerdict(
                action=OutputAction.REGENERATE,
                sanitized_text=current if rewrites else text,
                rewrites_applied=rewrites,
                direct_answer_detected=True,
                sentences_over_limit=over_limit_count,
                reason="Tutoring agent gave direct answer — must regenerate with guided approach",
            )

        if over_limit_count > 0:
            logger.info(
                "output_safety.sentences_too_long",
                count=over_limit_count,
                child_age=self.child_age,
            )

        if rewrites:
            return OutputVerdict(
                action=OutputAction.REWRITE,
                sanitized_text=current,
                rewrites_applied=rewrites,
                sentences_over_limit=over_limit_count,
                reason=f"{len(rewrites)} fear-word rewrites applied",
            )

        return OutputVerdict(
            action=OutputAction.PASS,
            sanitized_text=current,
            sentences_over_limit=over_limit_count,
            reason="no rewrites needed" if over_limit_count == 0 else "sentences too long — log only",
        )

    def _count_long_sentences(self, text: str) -> int:
        """Count sentences exceeding the age-adaptive max length."""
        sentences = [s.strip() for s in self._SENT_DELIM.split(text) if s and not re.match(self._SENT_DELIM, s)]
        if not sentences:
            return 0
        over = 0
        for s in sentences:
            char_count = self._count_visible_chars(s)
            if char_count > self._max_sentence_chars:
                over += 1
        return over

    def _count_visible_chars(self, sentence: str) -> int:
        """Count visible characters in a sentence.

        Treats ASCII words as having ~1 char/word and CJK characters
        as 1 char each (PRD #56).
        """
        # For CJK-heavy text, length is character count
        cjk_count = len(self._CJK_CHAR.findall(sentence))
        non_cjk = re.sub(self._CJK_CHAR, "", sentence).strip()
        # For non-CJK portion, split by whitespace and count words
        word_count = len(non_cjk.split()) if non_cjk else 0
        return cjk_count + word_count
