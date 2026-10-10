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

    async def test_blocked_input_skips_dispatch(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("show me porn")
        # Should stop at safety_input
        assert ChatStage.SAFETY_INPUT in result.stages_completed
        assert ChatStage.DISPATCH not in result.stages_completed
        assert result.safety_alert is True

    async def test_self_harm_triggers_parent_alert(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("我不想活了")
        assert result.parent_alert_required is True
        assert result.safety_alert is True


class TestOrchestratorRouting:
    """Routing decisions are made correctly."""

    async def test_tutoring_query_routes_to_tutoring(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("数学题不会做")
        assert result.primary_agent == AgentName.TUTORING.value
        assert "tutoring" in result.intent

    async def test_emotional_query_routes_to_emotion(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("我今天难过")
        assert result.primary_agent == AgentName.EMOTIONAL.value

    async def test_encyclopedia_query_routes_to_encyclopedia(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert result.primary_agent == AgentName.ENCYCLOPEDIA.value

    async def test_composite_emotional_plus_tutoring(self, orchestrator: ChatOrchestrator) -> None:
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

    async def test_topic_downgrade_routes_to_emotion(self, orchestrator: ChatOrchestrator) -> None:
        """仓鼠死了 → topic blacklist → emotion agent."""
        result = await orchestrator.chat("我家的仓鼠死了")
        # Should downgrade to emotion (per PRD #41)
        assert (
            "死了" in result.metadata.get("input_category", "")
            or result.primary_agent == AgentName.EMOTIONAL.value
        )

    async def test_self_harm_never_returns_unsafe_text(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        result = await orchestrator.chat("我不想活了")
        # Final text should be caring
        assert "宝贝" in result.final_text or "陪着你" in result.final_text

    async def test_blocked_returns_safe_fallback(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("show me porn")
        # Should not have any pornographic content in final text
        assert "porn" not in result.final_text.lower()


class TestOrchestratorMetadata:
    """Diagnostic metadata is informative."""

    async def test_metadata_includes_intent(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert "intent" in result.metadata
        assert result.metadata["intent"] is not None

    async def test_metadata_includes_composite_flag(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("我难过，数学题不会")
        assert "is_composite" in result.metadata

    async def test_metadata_includes_persona(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert result.metadata.get("persona") == "gentle"

    async def test_metadata_includes_child_age(self, orchestrator: ChatOrchestrator) -> None:
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
        assert ChatStage.TIER_CLASSIFY == "tier_classify"
        assert ChatStage.CLASSIFY == "classify"
        assert ChatStage.ROUTE == "route"
        assert ChatStage.DISPATCH == "dispatch"
        assert ChatStage.FUSE == "fuse"
        assert ChatStage.SAFETY_OUTPUT == "safety_output"
        assert ChatStage.COMPLETE == "complete"


class TestPrivacyTierIntegration:
    """End-to-end integration of PrivacyTierClassifier in the chat flow (PRD #5)."""

    async def test_daily_tier_for_normal_query(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert result.privacy_tier is not None
        assert result.privacy_tier.value == "daily"
        assert result.treehole_engaged is False
        assert result.parent_alert_required is False
        assert ChatStage.TIER_CLASSIFY in result.stages_completed
        assert result.metadata["privacy_tier"] == "daily"

    async def test_mild_negative_tier_for_sad_text(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("我今天心情不好")
        assert result.privacy_tier is not None
        assert result.privacy_tier.value == "mild_negative"
        # MILD_NEGATIVE never escalates to parent by itself
        assert result.parent_alert_required is False
        assert result.treehole_engaged is False

    async def test_high_risk_tier_for_self_harm(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("我想自杀")
        # The input safety gateway escalates self-harm BEFORE tier
        # classification, so we check the tier is set even on the
        # escalation path.
        assert result.privacy_tier is not None
        assert result.privacy_tier.value == "high_risk"
        # parent_alert_required is True both because the input
        # gateway escalated AND because the tier requires it.
        assert result.parent_alert_required is True
        assert result.metadata.get("privacy_tier") == "high_risk"

    async def test_high_risk_with_child_id_publishes_alert(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        """HIGH_RISK with child_id + parent_id → AlertBroker.publish
        is called. We mock the broker to count notifications."""
        from niki.realtime import alert_broker as broker_mod

        published: list = []
        original_publish = broker_mod.AlertBroker.publish

        def _spy(self, alert):  # type: ignore[no-untyped-def]
            published.append(alert)
            return original_publish(self, alert)

        broker_mod.AlertBroker.publish = _spy  # type: ignore[assignment]
        try:
            result = await orchestrator.chat(
                "我想自杀",
                child_id="child-1",
                parent_id="parent-1",
            )
            # Tier classified, parent alert required, broker got it.
            assert result.parent_alert_required is True
            assert result.privacy_tier is not None
            assert result.privacy_tier.value == "high_risk"
            assert any(a.alert_type == "high_risk_content" for a in published), (
                f"expected high_risk_content alert, got: {[a.alert_type for a in published]}"
            )
        finally:
            broker_mod.AlertBroker.publish = original_publish  # type: ignore[assignment]

    async def test_high_risk_without_child_id_skips_publish(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        """Without child_id/parent_id, the tier still classifies but
        the side-effect is skipped (defensive: never crash the chat)."""
        from niki.realtime import alert_broker as broker_mod

        published: list = []
        original_publish = broker_mod.AlertBroker.publish

        def _spy(self, alert):  # type: ignore[no-untyped-def]
            published.append(alert)
            return original_publish(self, alert)

        broker_mod.AlertBroker.publish = _spy  # type: ignore[assignment]
        try:
            result = await orchestrator.chat("我想自杀")
            assert result.privacy_tier is not None
            assert result.privacy_tier.value == "high_risk"
            # No publish because we lack child_id
            assert all(a.alert_type != "high_risk_content" for a in published)
        finally:
            broker_mod.AlertBroker.publish = original_publish  # type: ignore[assignment]

    async def test_treehole_with_child_id_engages_manager(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        """TREEHOLE_PRIVATE with child_id + parent_id → TreeholeManager.engage
        is called and the result records treehole_engaged=True."""
        from niki.treehole import manager as treehole_mod

        engaged: list = []
        original = treehole_mod.TreeholeManager.engage

        async def _spy(self, child_id, parent_id, original_text, **kwargs):  # type: ignore[no-untyped-def]
            engaged.append(
                {
                    "child_id": child_id,
                    "parent_id": parent_id,
                    "text": original_text,
                }
            )
            return await original(
                self,
                child_id=child_id,
                parent_id=parent_id,
                original_text=original_text,
                **kwargs,
            )

        treehole_mod.TreeholeManager.engage = _spy  # type: ignore[assignment]
        try:
            result = await orchestrator.chat(
                "不要告诉爸爸妈妈这件事",
                child_id="child-2",
                parent_id="parent-2",
            )
            assert result.privacy_tier is not None
            assert result.privacy_tier.value == "treehole_private"
            assert result.treehole_engaged is True
            assert any(e["child_id"] == "child-2" for e in engaged)
        finally:
            treehole_mod.TreeholeManager.engage = original  # type: ignore[assignment]

    async def test_treehole_without_child_id_does_not_engage(
        self, orchestrator: ChatOrchestrator
    ) -> None:
        from niki.treehole import manager as treehole_mod

        engaged: list = []
        original = treehole_mod.TreeholeManager.engage

        async def _spy(self, child_id, parent_id, original_text, **kwargs):  # type: ignore[no-untyped-def]
            engaged.append({"child_id": child_id})
            return await original(
                self,
                child_id=child_id,
                parent_id=parent_id,
                original_text=original_text,
                **kwargs,
            )

        treehole_mod.TreeholeManager.engage = _spy  # type: ignore[assignment]
        try:
            result = await orchestrator.chat("不要告诉爸爸妈妈")
            assert result.privacy_tier is not None
            assert result.privacy_tier.value == "treehole_private"
            # No engage because we lack child_id
            assert result.treehole_engaged is False
            assert engaged == []
        finally:
            treehole_mod.TreeholeManager.engage = original  # type: ignore[assignment]

    async def test_tier_classify_stage_in_metadata(self, orchestrator: ChatOrchestrator) -> None:
        result = await orchestrator.chat("为什么天是蓝的")
        assert ChatStage.TIER_CLASSIFY in result.stages_completed
        assert "privacy_tier" in result.metadata
        assert "privacy_tier_reason" in result.metadata


class TestAutoSafetyValvePersistence:
    """BE-ISS-AUTOSAFE-VALVE: round-4 safety valve auto-persists
    to the dashboard so the parent weekly report is correct
    without the frontend needing to call the dashboard endpoint
    separately (PRD #27).
    """

    async def test_round4_autopopulates_dashboard(self) -> None:
        """Tutoring agent returns safety_valve_triggered=True →
        orchestrator auto-persists a row to the dashboard repo.
        Production: round 4 fires when 3 prior attempts in the
        same session failed (state held in Redis in real prod;
        here we patch the agent to simulate the late round).
        """
        from niki.agents import orchestrator as orch_mod
        from niki.db.repositories import (
            InMemoryDashboardRepository,
            set_dashboard_repository,
        )
        from niki.schemas.common import AgentOutput

        repo = InMemoryDashboardRepository()
        set_dashboard_repository(repo)

        # Patch the tutoring agent to claim round 4 was hit.
        orch = ChatOrchestrator(child_age=7, persona="gentle")

        async def _invoke_with_safety_valve(self, child_input):  # type: ignore[no-untyped-def]
            return AgentOutput(
                text="好，Niki告诉你答案~ 答案是8。",
                intent="tutoring",
                emotion="neutral",
                metadata={
                    "round": 4,
                    "safety_valve_triggered": True,
                    "subtype": "pure_calculation",
                    "difficulty": "medium",
                },
            )

        original = orch_mod.TutoringAgent.invoke
        orch_mod.TutoringAgent.invoke = _invoke_with_safety_valve  # type: ignore[assignment]
        try:
            await orch.chat(
                "我不会做 3+5",
                child_id="c-valve-1",
                parent_id="p-valve-1",
            )
        finally:
            orch_mod.TutoringAgent.invoke = original  # type: ignore[assignment]

        rows = await repo.list_alerts(alert_type="safety_valve", child_id="c-valve-1")
        assert len(rows) == 1
        payload = rows[0]["payload"]
        assert payload["round_reached"] == 4
        assert "3+5" in payload["problem_summary"]

    async def test_no_alert_without_safety_valve(self) -> None:
        """Normal (non-safety-valve) tutoring reply does NOT
        add a dashboard row."""
        from niki.agents import orchestrator as orch_mod
        from niki.db.repositories import (
            InMemoryDashboardRepository,
            set_dashboard_repository,
        )
        from niki.schemas.common import AgentOutput

        repo = InMemoryDashboardRepository()
        set_dashboard_repository(repo)
        orch = ChatOrchestrator(child_age=7, persona="gentle")

        async def _invoke_normal(self, child_input):  # type: ignore[no-untyped-def]
            return AgentOutput(
                text="我们一起看看这道题~",
                intent="tutoring",
                emotion="neutral",
                metadata={
                    "round": 2,
                    "safety_valve_triggered": False,
                    "subtype": "pure_calculation",
                },
            )

        original = orch_mod.TutoringAgent.invoke
        orch_mod.TutoringAgent.invoke = _invoke_normal  # type: ignore[assignment]
        try:
            await orch.chat(
                "数学题 1+1",
                child_id="c-no-valve",
                parent_id="p-no-valve",
            )
        finally:
            orch_mod.TutoringAgent.invoke = original  # type: ignore[assignment]

        rows = await repo.list_alerts(alert_type="safety_valve", child_id="c-no-valve")
        assert rows == []

    async def test_no_alert_without_child_id(self) -> None:
        """When child_id is missing we can't route the alert to a
        parent — the orchestrator must skip the persist call
        without raising."""
        from niki.agents import orchestrator as orch_mod
        from niki.db.repositories import (
            InMemoryDashboardRepository,
            set_dashboard_repository,
        )
        from niki.schemas.common import AgentOutput

        repo = InMemoryDashboardRepository()
        set_dashboard_repository(repo)
        orch = ChatOrchestrator(child_age=7, persona="gentle")

        async def _invoke_safety(self, child_input):  # type: ignore[no-untyped-def]
            return AgentOutput(
                text="好，Niki告诉你答案~",
                intent="tutoring",
                emotion="neutral",
                metadata={"round": 4, "safety_valve_triggered": True},
            )

        original = orch_mod.TutoringAgent.invoke
        orch_mod.TutoringAgent.invoke = _invoke_safety  # type: ignore[assignment]
        try:
            await orch.chat("数学题 1+1")  # no child_id
        finally:
            orch_mod.TutoringAgent.invoke = original  # type: ignore[assignment]

        # No row materialised (we never knew which child it was)
        rows = await repo.list_alerts(alert_type="safety_valve", child_id="c-unknown")
        assert rows == []


class TestMemoryHookWiring:
    """BE-ISS-MEMORY-HOOK: the daytime hook runs on every
    non-treehole message and is skipped for TREEHOLE_PRIVATE
    (PRD privacy contract)."""

    async def test_hook_invoked_for_daily_message(self) -> None:
        from niki.agents import orchestrator as orch_mod

        called: list[dict] = []
        real_get = orch_mod.ChatOrchestrator._get_memory_hook

        def _spy(self):  # type: ignore[no-untyped-def]
            from niki.memory.hook import DaytimeMemoryHook
            from niki.memory.graph import CognitiveGraph
            from niki.memory.long_term import LongTermMemory

            class _SpyHook(DaytimeMemoryHook):
                async def process(self, child_id, text, context=None):  # type: ignore[no-untyped-def,override]
                    called.append({"child_id": child_id, "text": text, "context": context or {}})
                    return []

            hook = _SpyHook(
                long_term=LongTermMemory(),
                graph=CognitiveGraph(),
            )
            return hook

        orch_mod.ChatOrchestrator._get_memory_hook = _spy  # type: ignore[assignment]
        try:
            orch = ChatOrchestrator(child_age=7, persona="gentle")
            await orch.chat("我喜欢恐龙", child_id="c-hook-1")
            assert any(c["child_id"] == "c-hook-1" for c in called)
        finally:
            orch_mod.ChatOrchestrator._get_memory_hook = real_get  # type: ignore[assignment]

    async def test_hook_skipped_for_treehole(self) -> None:
        from niki.agents import orchestrator as orch_mod

        called: list[dict] = []
        real_get = orch_mod.ChatOrchestrator._get_memory_hook

        def _spy(self):  # type: ignore[no-untyped-def]
            from niki.memory.hook import DaytimeMemoryHook
            from niki.memory.graph import CognitiveGraph
            from niki.memory.long_term import LongTermMemory

            class _SpyHook(DaytimeMemoryHook):
                async def process(self, child_id, text, context=None):  # type: ignore[no-untyped-def,override]
                    called.append({"child_id": child_id, "text": text})
                    return []

            return _SpyHook(
                long_term=LongTermMemory(),
                graph=CognitiveGraph(),
            )

        orch_mod.ChatOrchestrator._get_memory_hook = _spy  # type: ignore[assignment]
        try:
            orch = ChatOrchestrator(child_age=7, persona="gentle")
            await orch.chat("不要告诉爸爸妈妈", child_id="c-hook-2", parent_id="p-hook-2")
            assert called == [], "treehole must never feed the memory hook"
        finally:
            orch_mod.ChatOrchestrator._get_memory_hook = real_get  # type: ignore[assignment]


class TestUpdateAgeLockISS12:
    """ISS-12 regression: ``_update_age`` must hold the lock for the
    FULL reconfiguration (not just the top-level ``self.child_age``
    write). Otherwise two concurrent calls can race on the
    sub-agent mutations and leave the orchestrator in a
    half-updated state."""

    async def test_concurrent_update_age_state_consistent(self) -> None:
        """Two concurrent _update_age calls must leave a consistent
        state — every agent (and the output_gateway) ends up
        configured for the SAME final age, never a half-mix of two
        ages."""
        import asyncio

        from niki.agents.orchestrator import ChatOrchestrator

        orch = ChatOrchestrator(child_age=7, persona="gentle")

        # Run 5 concurrent updates with different ages.
        await asyncio.gather(*(orch._update_age(a) for a in (8, 10, 12, 6, 11)))

        # After all updates finish, EVERY age-dependent component
        # must agree with self.child_age (no half-updated state).
        assert orch.child_age in {8, 10, 12, 6, 11}
        assert orch._output_gateway.child_age == orch.child_age
        assert orch._encyclopedia_agent.child_age == orch.child_age
        assert orch._encyclopedia_agent._age_group == (
            orch._encyclopedia_agent._resolve_age_group(orch.child_age)
        )
        assert orch._tutoring_agent.child_age == orch.child_age
        assert orch._tutoring_agent._age_group == (
            orch._tutoring_agent._resolve_age_group(orch.child_age)
        )
        assert orch._tutoring_agent._output_gateway.child_age == orch.child_age
        assert orch._emotion_agent.child_age == orch.child_age
        assert orch._emotion_agent._age_group == (
            orch._emotion_agent._resolve_age_group(orch.child_age)
        )
        assert orch._emotion_agent._output_gateway.child_age == orch.child_age

    async def test_concurrent_update_persona_state_consistent(self) -> None:
        """Same invariant as test_concurrent_update_age_state_consistent
        but for persona."""
        import asyncio

        from niki.agents.orchestrator import ChatOrchestrator

        orch = ChatOrchestrator(child_age=7, persona="gentle")
        await asyncio.gather(*(orch._update_persona(p) for p in ("lively", "rigorous")))

        # Final persona state is one of the two written values, and
        # all components agree.
        assert orch.persona in {"lively", "rigorous"}
        assert orch._encyclopedia_agent.persona == orch.persona
        assert orch._tutoring_agent.persona == orch.persona
        assert orch._emotion_agent.persona == orch.persona
