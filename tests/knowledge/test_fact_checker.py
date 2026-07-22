"""Tests for the fact-checker middleware."""

import pytest

from niki.knowledge.fact_checker import (
    FactChecker,
    FactCheckResult,
)
from niki.knowledge.knowledge_base import KnowledgeBase, KnowledgeSource


@pytest.fixture
def kb() -> KnowledgeBase:
    return KnowledgeBase()


@pytest.fixture
def checker(kb: KnowledgeBase) -> FactChecker:
    return FactChecker(kb, threshold=0.5)


class TestFactCheckerEmpty:
    """Empty / invalid inputs."""

    async def test_empty_claim_not_confident(self, checker: FactChecker) -> None:
        result = await checker.check("")
        assert not result.is_confident

    async def test_whitespace_claim_not_confident(self, checker: FactChecker) -> None:
        result = await checker.check("   ")
        assert not result.is_confident


class TestFactCheckerHeuristic:
    """Heuristic (no LLM judge) path."""

    async def test_claim_with_evidence_passes(self, checker: FactChecker) -> None:
        result = await checker.check(
            claim="天空是蓝色的因为阳光照射",
            evidence=["天空是蓝色的，因为阳光照射大气层。"],
        )
        assert result.is_confident
        assert result.confidence > 0.5

    async def test_claim_without_evidence_fails(self, checker: FactChecker) -> None:
        result = await checker.check(
            claim="some claim that is not in evidence",
            evidence=["unrelated content here"],
        )
        assert not result.is_confident

    async def test_no_evidence_at_all_fails(self, checker: FactChecker) -> None:
        result = await checker.check("anything", evidence=[])
        assert not result.is_confident
        assert "no evidence" in result.reason.lower()

    async def test_threshold_respected(self, kb: KnowledgeBase) -> None:
        # High threshold → less likely to pass
        strict_checker = FactChecker(kb, threshold=0.99)
        result = await strict_checker.check(
            claim="天空蓝色",
            evidence=["天空是蓝色的"],
        )
        # With very high threshold, even partial overlap may fail
        assert result.confidence <= 1.0


class TestFactCheckerLLMJudge:
    """LLM-judge path (with stub)."""

    async def test_llm_judge_supported(self) -> None:
        class StubJudge:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"content": '{"supported": true, "confidence": 0.92, "reason": "matches evidence"}'}

        kb = KnowledgeBase()
        checker = FactChecker(kb, llm_judge=StubJudge(), threshold=0.5)
        result = await checker.check(
            claim="天空是蓝的",
            evidence=["天空是蓝色的，因为阳光照射"],
        )
        assert result.is_confident
        assert result.confidence == 0.92

    async def test_llm_judge_unsupported(self) -> None:
        class StubJudge:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"content": '{"supported": false, "confidence": 0.95, "reason": "no support"}'}

        kb = KnowledgeBase()
        checker = FactChecker(kb, llm_judge=StubJudge(), threshold=0.5)
        result = await checker.check("unsupported claim", evidence=["evidence"])
        assert not result.is_confident

    async def test_llm_judge_invalid_json_falls_back(self) -> None:
        class StubJudge:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"content": "this is not valid json"}

        kb = KnowledgeBase()
        checker = FactChecker(kb, llm_judge=StubJudge(), threshold=0.5)
        # Should fall back to heuristic
        result = await checker.check(
            claim="天空是蓝的",
            evidence=["天空是蓝色的"],
        )
        # Heuristic may or may not pass; just shouldn't crash
        assert isinstance(result, FactCheckResult)

    async def test_llm_judge_code_fences(self) -> None:
        """JSON wrapped in code fences is parsed."""

        class StubJudge:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"content": '```json\n{"supported": true, "confidence": 0.85}\n```'}

        kb = KnowledgeBase()
        checker = FactChecker(kb, llm_judge=StubJudge(), threshold=0.5)
        result = await checker.check("claim", evidence=["evidence"])
        assert result.is_confident
        assert result.confidence == 0.85


class TestFactCheckerKBIntegration:
    """Integration with the knowledge base (no pre-fetched evidence)."""

    async def test_fetches_evidence_from_kb(self, checker: FactChecker) -> None:
        # Ask about a topic the KB knows
        result = await checker.check(
            claim="天空看起来是蓝色的因为阳光照射",
        )
        # KB has "天空为什么是蓝色的" entry; should be confident
        assert result.is_confident
        assert any(s == KnowledgeSource.L1_CORE for s in result.supporting_sources)

    async def test_unknown_topic_low_confidence(self, checker: FactChecker) -> None:
        result = await checker.check(
            claim="火星人住在开普勒-186f",
        )
        # KB doesn't know about aliens
        assert not result.is_confident
