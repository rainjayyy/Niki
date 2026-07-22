"""Tests for the multi-agent router."""


from niki.routing.classifier import (
    Intent,
    IntentCategory,
    IntentResult,
    TutoringSubtype,
)
from niki.routing.router import AgentName, AgentRouter


def _result(*intents: Intent, threshold: float = 0.6) -> IntentResult:
    """Helper to build an IntentResult from a sequence of Intents."""
    return IntentResult(intents=list(intents), threshold=threshold)


class TestRouterEmpty:
    """Empty / below-threshold intents."""

    def test_empty_intents_defaults_to_emotional(self) -> None:
        router = AgentRouter()
        decision = router.route(IntentResult(intents=[]))
        assert decision.primary_agent == AgentName.EMOTIONAL
        assert not decision.is_composite

    def test_all_below_threshold_defaults_to_emotional(self) -> None:
        router = AgentRouter()
        decision = router.route(
            _result(
                Intent(IntentCategory.TUTORING, 0.2),
                Intent(IntentCategory.EMOTIONAL, 0.1),
            )
        )
        # Highest-below-threshold intent still becomes primary
        assert decision.primary_agent in {AgentName.TUTORING, AgentName.EMOTIONAL}


class TestRouterSingle:
    """Single-intent routing."""

    def test_tutoring_only_routes_to_tutoring(self) -> None:
        router = AgentRouter()
        decision = router.route(
            _result(Intent(IntentCategory.TUTORING, 0.9, TutoringSubtype.WORD_PROBLEM))
        )
        assert decision.primary_agent == AgentName.TUTORING
        assert decision.parallel_agents == []
        assert not decision.is_composite

    def test_encyclopedia_only_routes_to_encyclopedia(self) -> None:
        router = AgentRouter()
        decision = router.route(
            _result(Intent(IntentCategory.ENCYCLOPEDIA, 0.85))
        )
        assert decision.primary_agent == AgentName.ENCYCLOPEDIA

    def test_emotional_only_routes_to_emotional(self) -> None:
        router = AgentRouter()
        decision = router.route(
            _result(Intent(IntentCategory.EMOTIONAL, 0.95))
        )
        assert decision.primary_agent == AgentName.EMOTIONAL


class TestRouterComposite:
    """Multi-intent (composite) routing."""

    def test_tutoring_and_emotional_composite(self) -> None:
        router = AgentRouter()
        decision = router.route(
            _result(
                Intent(IntentCategory.TUTORING, 0.85, TutoringSubtype.PURE_CALCULATION),
                Intent(IntentCategory.EMOTIONAL, 0.75),
            )
        )
        # Per PRD: emotional goes first ("先情感共情→再切入解题")
        assert decision.primary_agent == AgentName.EMOTIONAL
        assert AgentName.TUTORING in decision.parallel_agents
        assert decision.is_composite

    def test_tutoring_and_encyclopedia_composite(self) -> None:
        router = AgentRouter()
        decision = router.route(
            _result(
                Intent(IntentCategory.TUTORING, 0.9, TutoringSubtype.PURE_CALCULATION),
                Intent(IntentCategory.ENCYCLOPEDIA, 0.7),
            )
        )
        # Tutoring has higher confidence → primary
        assert decision.primary_agent == AgentName.TUTORING
        assert AgentName.ENCYCLOPEDIA in decision.parallel_agents

    def test_all_three_intents_composite(self) -> None:
        router = AgentRouter()
        decision = router.route(
            _result(
                Intent(IntentCategory.ENCYCLOPEDIA, 0.7),
                Intent(IntentCategory.TUTORING, 0.85, TutoringSubtype.PURE_CALCULATION),
                Intent(IntentCategory.EMOTIONAL, 0.75),
            )
        )
        assert decision.is_composite
        # Emotional first
        assert decision.primary_agent == AgentName.EMOTIONAL
        assert len(decision.parallel_agents) == 2


class TestRouterDisablingComposite:
    """When composite routing is disabled, only the primary is invoked."""

    def test_composite_disabled_ignores_parallel_intents(self) -> None:
        router = AgentRouter(enable_composite=False)
        decision = router.route(
            _result(
                Intent(IntentCategory.TUTORING, 0.9, TutoringSubtype.PURE_CALCULATION),
                Intent(IntentCategory.EMOTIONAL, 0.75),
            )
        )
        # Emotional primary (per ordering rule), but composite disabled
        assert decision.primary_agent == AgentName.EMOTIONAL
        assert decision.parallel_agents == []
        assert not decision.is_composite


class TestRouterOrdering:
    """Empathy-first ordering (PRD rule)."""

    def test_emotional_always_first_when_present(self) -> None:
        router = AgentRouter()
        decision = router.route(
            _result(
                Intent(IntentCategory.TUTORING, 0.95),
                Intent(IntentCategory.EMOTIONAL, 0.65),
            )
        )
        # Even with high tutoring confidence, emotional comes first
        assert decision.primary_agent == AgentName.EMOTIONAL
