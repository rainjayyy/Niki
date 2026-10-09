"""Intent classifier — doubao-powered two-level classification.

PRD #2-class spec:
  Level 1:  encyclopedia | tutoring | emotional | composite
  Level 2 (only when tutoring): pure_calculation | word_problem | visual_problem | open_ended

Design:
- Async (calls doubao)
- Returns structured IntentResult with confidence per category
- Composite intent is detected when MULTIPLE categories have high confidence
- Falls back to rule-based classification when API key not configured
  (lets us develop + test without burning real tokens)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from niki.infrastructure.llm.base import BaseLLMClient
from niki.infrastructure.llm.doubao import TASK_CLASSIFICATION, DoubaoClient

logger = structlog.get_logger(__name__)


# Confidence threshold for considering a category "active"
# (above this, the category contributes to composite-intent detection)
DEFAULT_CONFIDENCE_THRESHOLD = 0.6


class IntentCategory(StrEnum):
    """Top-level intent categories from the classifier."""

    ENCYCLOPEDIA = "encyclopedia"
    TUTORING = "tutoring"
    EMOTIONAL = "emotional"
    COMPOSITE = "composite"


class TutoringSubtype(StrEnum):
    """Sub-classification of tutoring intents (PRD)."""

    PURE_CALCULATION = "pure_calculation"
    WORD_PROBLEM = "word_problem"
    VISUAL_PROBLEM = "visual_problem"
    OPEN_ENDED = "open_ended"


@dataclass(frozen=True)
class Intent:
    """A single detected intent with confidence."""

    category: IntentCategory
    confidence: float
    tutoring_subtype: TutoringSubtype | None = None

    def __repr__(self) -> str:
        subtype = f"/{self.tutoring_subtype}" if self.tutoring_subtype else ""
        return f"Intent({self.category}{subtype}, {self.confidence:.2f})"


@dataclass(frozen=True)
class CompositeIntent:
    """When multiple intents are detected, this bundles them."""

    intents: list[Intent]
    threshold: float

    @property
    def primary(self) -> Intent | None:
        """The highest-confidence intent."""
        if not self.intents:
            return None
        return max(self.intents, key=lambda i: i.confidence)

    @property
    def is_composite(self) -> bool:
        """True when more than one category passes the threshold."""
        return len([i for i in self.intents if i.confidence >= self.threshold]) > 1


@dataclass(frozen=True)
class IntentResult:
    """Final classification result."""

    intents: list[Intent]  # sorted desc by confidence
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    raw_text: str = ""

    @property
    def primary(self) -> Intent | None:
        return self.intents[0] if self.intents else None

    @property
    def is_composite(self) -> bool:
        return CompositeIntent(self.intents, self.threshold).is_composite

    def above_threshold(self) -> list[Intent]:
        return [i for i in self.intents if i.confidence >= self.threshold]


class IntentClassifier:
    """Classifies child input into one or more intent categories.

    Usage:
        classifier = IntentClassifier()
        result = await classifier.classify("我数学题不会做")
        if result.primary.category == IntentCategory.TUTORING:
            ...
    """

    # Heuristic keywords for the rule-based fallback
    _TUTORING_KEYWORDS = {
        "数学题",
        "算",
        "题",
        "怎么做",
        "解答",
        "不会做",
        "卡住",
        # Added 2026-10-09 to catch math-only inputs without Chinese keywords
        "等于", "等于多少", "多少", "几",
        "加", "减", "乘", "除",
        "+", "-", "×", "÷",
        "equation", "math", "solve", "calculate", "how to",
    }
    _EMOTIONAL_KEYWORDS = {
        "不开心",
        "难过",
        "伤心",
        "生气",
        "委屈",
        "哭",
        "害怕",
        "sad",
        "happy",
        "angry",
        "scared",
        "upset",
        "lonely",
    }
    _ENCYCLOPEDIA_KEYWORDS = {
        "为什么",
        "是什么",
        "怎么",
        "哪个",
        "什么时候",
        "哪里",
        "what",
        "why",
        "when",
        "where",
        "which",
        "how",
    }

    def __init__(
        self,
        llm: BaseLLMClient | None = None,
        threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._llm = llm
        self.threshold = threshold

    async def classify(self, text: str) -> IntentResult:
        """Classify the input text into one or more intents.

        Uses the LLM when available; otherwise falls back to a
        rule-based heuristic (good for tests + dev without API keys).
        """
        if not text or not text.strip():
            return IntentResult(intents=[], raw_text=text)

        if self._llm is not None:
            try:
                return await self._classify_with_llm(text)
            except Exception as exc:
                logger.warning(
                    "intent_classifier.llm_failed",
                    error=str(exc),
                    fallback="rule_based",
                )

        return self._classify_rule_based(text)

    async def _classify_with_llm(self, text: str) -> IntentResult:
        """Use doubao to classify intents."""
        assert isinstance(self._llm, DoubaoClient) or self._llm is not None

        system_prompt = (
            "你是一个意图分类器。把孩子的输入分类到以下类别之一:\n"
            "- encyclopedia (百科问答：想知道是什么/为什么)\n"
            "- tutoring (作业辅导：数学题/解题)\n"
            "- emotional (情感陪伴：开心/难过/害怕)\n"
            "- composite (复合：同时包含多个意图)\n\n"
            "如果 tutoring，再细分为: pure_calculation, word_problem, "
            "visual_problem, open_ended.\n\n"
            "返回严格的 JSON 格式（不要其他文字）:\n"
            '{"intents": [{"category": "...", "confidence": 0.0-1.0, '
            '"tutoring_subtype": "..."}]}'
        )

        response = await self._llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=256,
            task_type=TASK_CLASSIFICATION,
        )

        parsed = self._parse_llm_response(response["content"])
        return self._build_result(parsed, text)

    def _classify_rule_based(self, text: str) -> IntentResult:
        """Rule-based fallback when LLM unavailable.

        Computes a confidence score per category based on keyword density.
        """
        text_lower = text.lower()
        scores: dict[str, float] = {
            IntentCategory.ENCYCLOPEDIA.value: 0.0,
            IntentCategory.TUTORING.value: 0.0,
            IntentCategory.EMOTIONAL.value: 0.0,
        }

        # Boost score per keyword match (capped)
        for kw in self._TUTORING_KEYWORDS:
            if kw.lower() in text_lower:
                scores[IntentCategory.TUTORING.value] += 0.35
        for kw in self._EMOTIONAL_KEYWORDS:
            if kw.lower() in text_lower:
                scores[IntentCategory.EMOTIONAL.value] += 0.45
        for kw in self._ENCYCLOPEDIA_KEYWORDS:
            if kw.lower() in text_lower:
                scores[IntentCategory.ENCYCLOPEDIA.value] += 0.25

        # Cap each at 0.95
        for k in scores:
            scores[k] = min(scores[k], 0.95)

        # Heuristic: if tutoring, pick a subtype (always, even below threshold)
        tutoring_subtype: TutoringSubtype | None = None
        if scores[IntentCategory.TUTORING.value] > 0.05:
            tutoring_subtype = self._infer_tutoring_subtype(text)

        intents: list[Intent] = []
        for cat_value, score in sorted(scores.items(), key=lambda x: -x[1]):
            if score > 0.05:  # ignore near-zero
                category = IntentCategory(cat_value)
                intents.append(
                    Intent(
                        category=category,
                        confidence=score,
                        tutoring_subtype=(
                            tutoring_subtype if category == IntentCategory.TUTORING else None
                        ),
                    )
                )

        return IntentResult(intents=intents, threshold=self.threshold, raw_text=text)

    @staticmethod
    def _infer_tutoring_subtype(text: str) -> TutoringSubtype:
        """Pick a tutoring subtype based on simple heuristics."""
        # Visual problem mentions an image / 看图 / picture
        if any(kw in text for kw in ("图", "图片", "看图", "picture", "image")):
            return TutoringSubtype.VISUAL_PROBLEM
        # Word problem mentions a story / 故事 / 应用题
        if any(kw in text for kw in ("应用题", "故事", "小明", "小华", "problem", "story")):
            return TutoringSubtype.WORD_PROBLEM
        # Pure calculation: digits and basic operators
        if re.search(r"[\d+\-*/×÷=]", text) or any(
            kw in text for kw in ("算", "加", "减", "乘", "除")
        ):
            return TutoringSubtype.PURE_CALCULATION
        return TutoringSubtype.OPEN_ENDED

    @staticmethod
    def _parse_llm_response(content: str) -> dict[str, Any]:
        """Extract JSON from the LLM response, tolerating code fences."""
        cleaned = content.strip()
        # Strip markdown code fences if present
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("intent_classifier.llm_invalid_json", content=cleaned[:200])
            return {"intents": []}

    @staticmethod
    def _build_result(parsed: dict[str, Any], text: str) -> IntentResult:
        """Convert parsed JSON into an IntentResult."""
        raw_intents = parsed.get("intents", [])
        intents: list[Intent] = []
        for entry in raw_intents:
            try:
                category = IntentCategory(entry.get("category", "").lower())
                confidence = float(entry.get("confidence", 0.0))
                subtype_str = entry.get("tutoring_subtype")
                subtype = TutoringSubtype(subtype_str) if subtype_str else None
                intents.append(
                    Intent(category=category, confidence=confidence, tutoring_subtype=subtype)
                )
            except (ValueError, KeyError) as exc:
                logger.warning("intent_classifier.parse_skip", entry=entry, error=str(exc))

        # Sort by confidence desc
        intents.sort(key=lambda i: -i.confidence)
        return IntentResult(intents=intents, raw_text=text)
