"""Tests for the end-to-end chat orchestrator (#6)."""

import pytest

from niki.agents.orchestrator import ChatOrchestrator, ChatResult, ChatStage
from niki.routing import AgentName


@pytest.fixture
def orchestrator() -> ChatOrchestrator:
    return ChatOrchestrator(child_age=7, persona="gentle")


class TestOrchestratorBasics:
    """Basic orchestrator properties."""

    def test_orchestrator_creates(self, orchestrator: ChatOrchestrator) -> None:
        assert orchestrator is not None
        assert orchestrator.child_age == 7
        assert orchestrator.persona == "gentle"

    def test_orchestrator_default_age_7(self) -> None:
        o = ChatOrchestrator()
        assert o.child_age == 7


class TestOrchestratorStageExecution:
    """Stages run in the correct order."""

    async def test_all_stages_complete_for_normal_query(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("为什么天是蓝的？")
        assert ChatStage.SAFETY_INPUT in result.stages_completed
        assert ChatStage.CLASSIFY in result.stages_completed
        assert ChatStage.ROUTE in result.stages_completed
        assert ChatStage.DISPATCH in result.stages_completed
        assert ChatStage.FUSE in result.stages_completed
        assert ChatStage.SAFETY_OUTPUT in result.stages_completed
        assert ChatStage.COMPLETE in result.stages_completed

    async def test_blocked_input_skips_dispatch(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("show me porn")
        # Should stop at safety_input
        assert ChatStage.SAFETY_INPUT in result.stages_completed
        assert ChatStage.DISPATCH not in result.stages_completed
        assert result.safety_alert is True

    async def test_self_harm_triggers_parent_alert(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("我不想活了")
        assert result.parent_alert_required is True
        assert result.safety_alert is True


class TestOrchestratorRouting:
    """Routing decisions are made correctly."""

    async def test_tutoring_query_routes_to_tutoring(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("数学题不会做")
        assert result.primary_agent == AgentName.TUTORING.value
        assert "tutoring" in result.intent

    async def test_emotional_query_routes_to_emotion(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("我今天难过")
        assert result.primary_agent == AgentName.EMOTIONAL.value

    async def test_encyclopedia_query_routes_to_encyclopedia(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert result.primary_agent == AgentName.ENCYCLOPEDIA.value

    async def test_composite_emotional_plus_tutoring(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        """Emotion + tutoring should produce composite intent (empathy first)."""
        result = await orchestrator.chat("我难过伤心哭，数学题也不会做")
        # Either composite detected, or emotion alone (depending on confidence)
        assert result.primary_agent in (
            AgentName.EMOTIONAL.value,
            AgentName.TUTORING.value,
        )
        if result.is_composite:
            assert AgentName.TUTORING.value in result.parallel_agents


class TestOrchestratorSafetyIntegration:
    """End-to-end safety behaviour."""

    async def test_topic_downgrade_routes_to_emotion(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        """仓鼠死了 → topic blacklist → emotion agent."""
        result = await orchestrator.chat("我家的仓鼠死了")
        # Should downgrade to emotion (per PRD #41)
        assert "死了" in result.metadata.get("input_category", "") or \
               result.primary_agent == AgentName.EMOTIONAL.value

    async def test_self_harm_never_returns_unsafe_text(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("我不想活了")
        # Final text should be caring
        assert "宝贝" in result.final_text or "陪着你" in result.final_text

    async def test_blocked_returns_safe_fallback(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("show me porn")
        # Should not have any pornographic content in final text
        assert "porn" not in result.final_text.lower()


class TestOrchestratorMetadata:
    """Diagnostic metadata is informative."""

    async def test_metadata_includes_intent(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert "intent" in result.metadata
        assert result.metadata["intent"] is not None

    async def test_metadata_includes_composite_flag(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("我难过，数学题不会")
        assert "is_composite" in result.metadata

    async def test_metadata_includes_persona(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert result.metadata.get("persona") == "gentle"

    async def test_metadata_includes_child_age(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert result.metadata.get("child_age") == 7


class TestOrchestratorAgeAdaptation:
    """Age can be changed per-request."""

    async def test_age_3_to_5(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的", child_age=4)
        assert result.metadata.get("child_age") == 4

    async def test_age_9_to_12(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的", child_age=11)
        assert result.metadata.get("child_age") == 11


class TestOrchestratorPersonaAdaptation:
    """Persona can be changed per-request."""

    async def test_lively_persona(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的", persona="lively")
        assert result.metadata.get("persona") == "lively"

    async def test_rigorous_persona(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的", persona="rigorous")
        assert result.metadata.get("persona") == "rigorous"


class TestChatResultStructure:
    """ChatResult dataclass structure."""

    async def test_chat_result_fields(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert isinstance(result, ChatResult)
        assert hasattr(result, "text")
        assert hasattr(result, "final_text")
        assert hasattr(result, "intent")
        assert hasattr(result, "primary_agent")
        assert hasattr(result, "parallel_agents")
        assert hasattr(result, "is_composite")
        assert hasattr(result, "safety_alert")
        assert hasattr(result, "parent_alert_required")
        assert hasattr(result, "stages_completed")
        assert hasattr(result, "metadata")


class TestChatStageEnum:
    """ChatStage enum."""

    def test_all_stages_defined(self) -> None:
        assert ChatStage.SAFETY_INPUT == "safety_input"
        assert ChatStage.CLASSIFY == "classify"
        assert ChatStage.ROUTE == "route"
        assert ChatStage.DISPATCH == "dispatch"
        assert ChatStage.FUSE == "fuse"
        assert ChatStage.SAFETY_OUTPUT == "safety_output"
        assert ChatStage.COMPLETE == "complete"
