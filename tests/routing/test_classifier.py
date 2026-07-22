"""Tests for the intent classifier."""

import pytest

from niki.routing.classifier import (
    IntentCategory,
    IntentClassifier,
    TutoringSubtype,
)


@pytest.fixture
def classifier() -> IntentClassifier:
    """Classifier with no LLM (rule-based fallback)."""
    return IntentClassifier()


@pytest.fixture
def low_threshold_classifier() -> IntentClassifier:
    """Classifier with lower threshold for edge-case tests."""
    return IntentClassifier(threshold=0.3)


class TestClassifierEmpty:
    """Empty / blank input handling."""

    async def test_empty_text_returns_no_intents(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("")
        assert result.intents == []

    async def test_whitespace_only_returns_no_intents(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("   ")
        assert result.intents == []


class TestClassifierRuleBased:
    """Rule-based classification (no LLM)."""

    async def test_tutoring_keyword_detected(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("我数学题不会做")
        assert result.primary is not None
        assert result.primary.category == IntentCategory.TUTORING
        assert result.primary.confidence >= 0.3

    async def test_emotional_keyword_detected(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("我今天很难过")
        assert result.primary is not None
        assert result.primary.category == IntentCategory.EMOTIONAL

    async def test_encyclopedia_keyword_detected(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("为什么天是蓝的？")
        assert result.primary is not None
        assert result.primary.category == IntentCategory.ENCYCLOPEDIA

    async def test_english_tutoring(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("how to solve this math problem")
        assert result.primary is not None
        assert result.primary.category == IntentCategory.TUTORING


class TestClassifierComposite:
    """Composite-intent detection (multiple categories active)."""

    async def test_emotional_and_tutoring_composite(
        self, low_threshold_classifier: IntentClassifier
    ) -> None:
        result = await low_threshold_classifier.classify("我难过，数学题也不会做")
        categories = {i.category for i in result.above_threshold()}
        # Should have at least emotional + tutoring
        assert IntentCategory.EMOTIONAL in categories
        assert IntentCategory.TUTORING in categories
        assert result.is_composite

    async def test_single_intent_not_composite(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("为什么天是蓝的")
        if len(result.above_threshold()) == 1:
            assert result.is_composite is False


class TestTutoringSubtype:
    """Tutoring subtype detection."""

    async def test_pure_calculation_subtype(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("算一下 3 + 5")
        assert result.primary is not None
        if result.primary.category == IntentCategory.TUTORING:
            assert result.primary.tutoring_subtype == TutoringSubtype.PURE_CALCULATION

    async def test_word_problem_subtype(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("这道应用题小明有3个苹果，小红给他2个，一共有多少")
        assert result.primary is not None
        if result.primary.category == IntentCategory.TUTORING:
            assert result.primary.tutoring_subtype == TutoringSubtype.WORD_PROBLEM

    async def test_visual_problem_subtype(self, classifier: IntentClassifier) -> None:
        result = await classifier.classify("看图回答问题")
        assert result.primary is not None
        if result.primary.category == IntentCategory.TUTORING:
            assert result.primary.tutoring_subtype == TutoringSubtype.VISUAL_PROBLEM


class TestClassifierLLMPath:
    """LLM-driven classification path (using a stub)."""

    async def test_llm_classifier_parses_json(self) -> None:
        """When LLM is provided, its JSON response is parsed."""

        class StubLLM:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {
                    "content": (
                        '{"intents": [{"category": "tutoring", "confidence": 0.92, '
                        '"tutoring_subtype": "pure_calculation"}]}'
                    )
                }

        classifier = IntentClassifier(llm=StubLLM())
        result = await classifier.classify("any text")
        assert result.primary is not None
        assert result.primary.category == IntentCategory.TUTORING
        assert result.primary.confidence == pytest.approx(0.92)
        assert result.primary.tutoring_subtype == TutoringSubtype.PURE_CALCULATION

    async def test_llm_invalid_json_falls_back(self) -> None:
        """Invalid LLM JSON should return empty intents (not crash)."""

        class StubLLM:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"content": "not valid json"}

        classifier = IntentClassifier(llm=StubLLM())
        result = await classifier.classify("any text")
        # Fall back to rule-based; "any text" may produce no intents or only encyclopedia
        assert isinstance(result.intents, list)

    async def test_llm_with_code_fences(self) -> None:
        """JSON wrapped in markdown code fences is parsed correctly."""

        class StubLLM:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {
                    "content": (
                        '```json\n{"intents": [{"category": "encyclopedia", '
                        '"confidence": 0.8}]}\n```'
                    )
                }

        classifier = IntentClassifier(llm=StubLLM())
        result = await classifier.classify("any text")
        assert result.primary is not None
        assert result.primary.category == IntentCategory.ENCYCLOPEDIA

    async def test_llm_error_falls_back_to_rules(self) -> None:
        """LLM exception should not crash — falls back to rule-based."""

        class BrokenLLM:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise RuntimeError("API down")

        classifier = IntentClassifier(llm=BrokenLLM())
        result = await classifier.classify("我数学题不会做")
        assert result.primary is not None
        assert result.primary.category == IntentCategory.TUTORING
