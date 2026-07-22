"""Tests for the fusion orchestrator."""

from niki.routing.classifier import IntentResult
from niki.routing.fusion import FusionOrchestrator, FusionStrategy
from niki.routing.router import AgentName, RoutingDecision
from niki.schemas.common import AgentOutput


def _decision_with_parallel(parallel: list[AgentName]) -> RoutingDecision:
    """Build a minimal RoutingDecision with the given parallel agents."""
    return RoutingDecision(
        primary_agent=AgentName.EMOTIONAL,
        parallel_agents=parallel,
        intent_result=IntentResult(intents=[]),
    )


class TestFusionSingleAgent:
    """Single-agent routing uses PICK_PRIMARY (no merging)."""

    def test_single_agent_uses_primary_only(self) -> None:
        orchestrator = FusionOrchestrator()
        decision = RoutingDecision(
            primary_agent=AgentName.TUTORING,
            intent_result=IntentResult(intents=[]),
        )
        outputs = {
            AgentName.TUTORING: AgentOutput(text="让我们一起看看题目~"),
        }
        fused = orchestrator.fuse(decision, outputs)
        assert fused.strategy == FusionStrategy.PICK_PRIMARY
        assert fused.text == "让我们一起看看题目~"
        assert fused.contributing_agents == [AgentName.TUTORING]

    def test_missing_output_returns_empty_text(self) -> None:
        orchestrator = FusionOrchestrator()
        decision = RoutingDecision(
            primary_agent=AgentName.TUTORING,
            intent_result=IntentResult(intents=[]),
        )
        fused = orchestrator.fuse(decision, {})
        assert fused.text == ""


class TestFusionComposite:
    """Composite routing uses CONCATENATE in routing order."""

    def test_emotional_first_then_tutoring(self) -> None:
        orchestrator = FusionOrchestrator()
        decision = _decision_with_parallel([AgentName.TUTORING])
        outputs = {
            AgentName.EMOTIONAL: AgentOutput(text="听起来你有点难过呀~"),
            AgentName.TUTORING: AgentOutput(text="我们一起看看这道题~"),
        }
        fused = orchestrator.fuse(decision, outputs)
        assert fused.strategy == FusionStrategy.CONCATENATE
        # Emotional comes first
        assert fused.text.index("难过") < fused.text.index("看看")
        assert len(fused.contributing_agents) == 2

    def test_all_three_agents(self) -> None:
        orchestrator = FusionOrchestrator()
        decision = _decision_with_parallel([AgentName.ENCYCLOPEDIA, AgentName.TUTORING])
        outputs = {
            AgentName.EMOTIONAL: AgentOutput(text="我在呢~"),
            AgentName.ENCYCLOPEDIA: AgentOutput(text="天是蓝的因为阳光照射~"),
            AgentName.TUTORING: AgentOutput(text="先看看这道题~"),
        }
        fused = orchestrator.fuse(decision, outputs)
        # Order: emotional (primary), encyclopedia (parallel[0]), tutoring (parallel[1])
        idx_emotional = fused.text.index("我在呢")
        idx_encyclopedia = fused.text.index("天是蓝的")
        idx_tutoring = fused.text.index("先看看这道题")
        assert idx_emotional < idx_encyclopedia < idx_tutoring

    def test_missing_parallel_agent_skipped(self) -> None:
        """Missing agent outputs should not crash fusion."""
        orchestrator = FusionOrchestrator()
        decision = _decision_with_parallel([AgentName.TUTORING])
        outputs = {
            AgentName.EMOTIONAL: AgentOutput(text="我在呢~"),
            # TUTORING intentionally missing
        }
        fused = orchestrator.fuse(decision, outputs)
        assert "我在呢" in fused.text
        assert AgentName.TUTORING not in fused.contributing_agents

    def test_empty_output_text_skipped(self) -> None:
        orchestrator = FusionOrchestrator()
        decision = _decision_with_parallel([AgentName.TUTORING])
        outputs = {
            AgentName.EMOTIONAL: AgentOutput(text="我在呢~"),
            AgentName.TUTORING: AgentOutput(text=""),  # empty
        }
        fused = orchestrator.fuse(decision, outputs)
        assert AgentName.TUTORING not in fused.contributing_agents


class TestFusionDiagnostics:
    """Fusion preserves diagnostic metadata."""

    def test_primary_output_recorded(self) -> None:
        orchestrator = FusionOrchestrator()
        decision = _decision_with_parallel([AgentName.TUTORING])
        emotional_out = AgentOutput(text="我在呢~", emotion="caring")
        outputs = {
            AgentName.EMOTIONAL: emotional_out,
            AgentName.TUTORING: AgentOutput(text="一起看~"),
        }
        fused = orchestrator.fuse(decision, outputs)
        assert fused.primary_output is emotional_out
        assert len(fused.parallel_outputs) == 1
        assert fused.parallel_outputs[0].text == "一起看~"
