"""Fact-checker middleware — PRD #43.

Target: ≥ 95% factual accuracy. Approach:
1. For each claim in the draft answer, search the KB / live API for evidence
2. Use LLM-as-judge to confirm the claim is supported by the evidence
3. Return a FactCheckResult with confidence + reason

When the LLM judge isn't available, falls back to a token-overlap heuristic
(claim must have substantial keyword overlap with the evidence).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog

from niki.knowledge.knowledge_base import KnowledgeBase, KnowledgeSource

logger = structlog.get_logger(__name__)


# Confidence threshold for treating a claim as "verified"
DEFAULT_CONFIDENCE_THRESHOLD = 0.3


@dataclass(frozen=True)
class FactCheckResult:
    """Result of fact-checking a claim against evidence."""

    is_confident: bool
    confidence: float
    reason: str
    supporting_sources: list[KnowledgeSource] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.supporting_sources is None:
            object.__setattr__(self, "supporting_sources", [])


class FactChecker:
    """Verifies LLM-generated claims against the knowledge base.

    Usage:
        checker = FactChecker(knowledge_base)
        result = await checker.check("天空是蓝的", evidence=["天空是蓝色的，因为..."])
        if not result.is_confident:
            # re-generate or admit "Niki is not sure"
    """

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        llm_judge: Any = None,
        threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._kb = knowledge_base
        self._llm_judge = llm_judge
        self._threshold = threshold

    async def check(
        self,
        claim: str,
        evidence: list[str] | None = None,
        use_live_search: bool = False,
    ) -> FactCheckResult:
        """Verify a claim against the KB.

        Strategy:
        1. If evidence is provided (e.g. from KB hits), check keyword overlap
        2. If use_live_search is True, fetch live evidence as well
        3. Optionally use an LLM judge for nuanced verification

        Args:
            claim: The LLM-generated draft answer to verify.
            evidence: Pre-fetched evidence strings (typically KB hits).
            use_live_search: Whether to also pull live-search evidence.

        Returns:
            FactCheckResult with is_confident and confidence.
        """
        if not claim or not claim.strip():
            return FactCheckResult(
                is_confident=False,
                confidence=0.0,
                reason="empty claim",
            )

        # If no evidence provided, fetch from KB
        if evidence is None or not evidence:
            hits = self._kb.search(claim, top_k=3)
            evidence = [h.content for h in hits]
            sources = [h.source for h in hits]
        else:
            sources = []

        if use_live_search:
            live_hits = await self._kb.live_search(claim, top_k=2)
            evidence = [*evidence, *[h.content for h in live_hits]]
            sources = [*sources, *[h.source for h in live_hits]]

        if not evidence:
            return FactCheckResult(
                is_confident=False,
                confidence=0.0,
                reason="no evidence found",
                supporting_sources=sources,
            )

        # Use LLM judge when available
        if self._llm_judge is not None:
            try:
                return await self._llm_judge_check(claim, evidence, sources)
            except Exception as exc:
                logger.warning(
                    "fact_checker.llm_judge_failed",
                    error=str(exc),
                    fallback="heuristic",
                )

        # Heuristic fallback: keyword overlap
        confidence, matched = self._heuristic_overlap(claim, evidence)
        is_confident = confidence >= self._threshold

        if is_confident:
            reason = f"overlap with {len(matched)} evidence pieces"
        else:
            reason = (
                f"insufficient overlap (confidence={confidence:.2f} "
                f"< threshold={self._threshold})"
            )

        return FactCheckResult(
            is_confident=is_confident,
            confidence=confidence,
            reason=reason,
            supporting_sources=sources,
        )

    async def _llm_judge_check(
        self,
        claim: str,
        evidence: list[str],
        sources: list[KnowledgeSource],
    ) -> FactCheckResult:
        """Use an LLM to judge the claim against evidence."""
        evidence_block = "\n".join(f"- {e}" for e in evidence[:5])
        prompt = (
            "你是事实核查专家。判断以下陈述是否被提供的证据支持。\n\n"
            f"陈述: {claim}\n\n"
            f"证据:\n{evidence_block}\n\n"
            "回答 JSON 格式 (不要其他文字):\n"
            '{"supported": true|false, "confidence": 0.0-1.0, "reason": "..."}'
        )

        response = await self._llm_judge.chat(  # type: ignore[union-attr]
            messages=[
                {"role": "system", "content": "你是事实核查专家。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=200,
        )

        # Parse JSON
        import json
        try:
            content = response["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE).strip()
            data = json.loads(content)
            is_confident = bool(data.get("supported", False))
            confidence = float(data.get("confidence", 0.0 if not is_confident else 1.0))
            reason = str(data.get("reason", "LLM judge"))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("fact_checker.parse_error", error=str(exc))
            return FactCheckResult(
                is_confident=False,
                confidence=0.0,
                reason="LLM judge returned unparseable response",
                supporting_sources=sources,
            )

        return FactCheckResult(
            is_confident=is_confident,
            confidence=confidence,
            reason=reason,
            supporting_sources=sources,
        )

    @staticmethod
    def _heuristic_overlap(claim: str, evidence: list[str]) -> tuple[float, set[str]]:
        """Score claim by keyword overlap with evidence.

        Returns:
            (confidence in [0,1], set of matched keywords)
        """
        claim_tokens = set(_tokenize(claim))
        if not claim_tokens:
            return 0.0, set()

        all_evidence_tokens: set[str] = set()
        for ev in evidence:
            all_evidence_tokens.update(_tokenize(ev))

        if not all_evidence_tokens:
            return 0.0, set()

        matched = claim_tokens & all_evidence_tokens
        # Use containment: what fraction of claim tokens appear in evidence
        confidence = len(matched) / len(claim_tokens)
        return confidence, matched


def _tokenize(text: str) -> list[str]:
    """Tokenize text for overlap analysis (CJK chars + English words)."""
    if not text:
        return []
    cjk = re.findall(r"[一-鿿]", text)
    english = re.findall(r"[a-zA-Z]+", text.lower())
    return cjk + english
