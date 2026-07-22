"""Tests for the Tutoring Agent end-to-end."""

import pytest

from niki.agents.tutoring import (
    STRATEGY_TEMPLATES,
    SUBTYPE_DIFFICULTY_DISCOUNT,
    ProblemState,
    Round,
    TutoringAgent,
)


class _StubLLM:
    """Returns the guided-discovery prompt verbatim (so the test can
    verify the agent's behavior end-to-end)."""

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.6,
        max_tokens: int = 256,
        **kwargs,
    ):
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if "「" in user_msg and "」" in user_msg:
            prompt = user_msg.split("「", 1)[1].split("」", 1)[0]
        else:
            prompt = "我们一起看看这道题~"
        return {"content": prompt}


@pytest.fixture
def agent() -> TutoringAgent:
    return TutoringAgent(llm=_StubLLM(), persona="gentle", child_age=7)


class TestTutoringAgentBasics:
    """Basic agent properties."""

    def test_agent_type(self, agent: TutoringAgent) -> None:
        assert agent.agent_type == "tutoring"

    def test_system_prompt_includes_no_external_tools(self, agent: TutoringAgent) -> None:
        prompt = agent.get_system_prompt_with_persona()
        # PRD #54: 零外部工具
        assert "计算器" in prompt or "外部工具" in prompt or "搜索" in prompt

    def test_system_prompt_includes_safety_valve(self, agent: TutoringAgent) -> None:
        prompt = agent.get_system_prompt_with_persona()
        # PRD #7: 安全阀
        assert "安全阀" in prompt or "4" in prompt

    def test_system_prompt_never_give_direct_answer(self, agent: TutoringAgent) -> None:
        prompt = agent.get_system_prompt_with_persona()
        # PRD: 永远不要直接给答案
        assert "直接给答案" in prompt or "不要直接" in prompt


class TestTutoringAgentProblemAnalysis:
    """Problem subtype detection + number extraction."""

    def test_pure_calculation_subtype(self, agent: TutoringAgent) -> None:
        assert agent._detect_subtype("3 + 5 = ?") == "pure_calculation"

    def test_word_problem_subtype(self, agent: TutoringAgent) -> None:
        assert agent._detect_subtype("小明有3个苹果") == "word_problem"

    def test_visual_problem_subtype(self, agent: TutoringAgent) -> None:
        assert agent._detect_subtype("看图回答问题") == "visual_problem"

    def test_open_ended_subtype(self, agent: TutoringAgent) -> None:
        assert agent._detect_subtype("为什么恐龙会消失") == "open_ended"

    def test_extract_numbers(self, agent: TutoringAgent) -> None:
        nums = agent._extract_numbers("3 + 5 = 8")
        assert nums == [3, 5, 8]

    def test_extract_numbers_empty(self, agent: TutoringAgent) -> None:
        assert agent._extract_numbers("no numbers here") == []


class TestTutoringAgentEmotion:
    """Emotion detection from input text."""

    def test_negative_emotion(self, agent: TutoringAgent) -> None:
        assert agent._detect_emotion("我不会，太难了") == "negative"

    def test_positive_emotion(self, agent: TutoringAgent) -> None:
        assert agent._detect_emotion("太简单啦") == "positive"

    def test_neutral_emotion(self, agent: TutoringAgent) -> None:
        assert agent._detect_emotion("算 3 + 5") == "neutral"


class TestTutoringAgentDifficulty:
    """Per-subtype difficulty assessment."""

    def test_pure_calculation_discount(self, agent: TutoringAgent) -> None:
        a = agent._assess_difficulty(ProblemState(subtype="pure_calculation"))
        assert a.discount == 1.0
        assert a.raw_accuracy == 0.5
        assert a.effective_difficulty == "medium"

    def test_word_problem_harder(self, agent: TutoringAgent) -> None:
        a = agent._assess_difficulty(ProblemState(subtype="word_problem"))
        # 0.5 * 0.7 = 0.35 → hard
        assert a.discount == 0.7
        assert a.effective_difficulty == "hard"

    def test_visual_problem_medium(self, agent: TutoringAgent) -> None:
        a = agent._assess_difficulty(ProblemState(subtype="visual_problem"))
        assert a.discount == 0.85

    def test_high_accuracy_child_is_easy(self) -> None:
        agent = TutoringAgent(
            llm=_StubLLM(),
            persona="gentle",
            child_age=7,
            child_history={"pure_calculation": 0.9, "word_problem": 0.9},
        )
        a = agent._assess_difficulty(ProblemState(subtype="pure_calculation"))
        # 0.9 * 1.0 = 0.9 → easy
        assert a.effective_difficulty == "easy"

    def test_difficulty_assessment_is_hard_property(self, agent: TutoringAgent) -> None:
        a = agent._assess_difficulty(ProblemState(subtype="word_problem"))
        assert a.is_hard


class TestTutoringAgentProblemState:
    """ProblemState round advancement."""

    def test_initial_state_round_none(self) -> None:
        state = ProblemState()
        assert state.last_round is None
        assert state.attempts == 0

    def test_first_call_advances_to_round_1(self) -> None:
        state = ProblemState()
        r = state.next_round()
        assert r == Round.SOCRATIC
        assert state.attempts == 1

    def test_third_call_advances_to_round_3(self) -> None:
        state = ProblemState()
        for _ in range(2):
            state.next_round()
        r = state.next_round()
        assert r == Round.VISUAL
        assert state.attempts == 3

    def test_fourth_call_triggers_safety_valve(self) -> None:
        state = ProblemState()
        for _ in range(3):
            state.next_round()
        r = state.next_round()
        assert r == Round.SAFETY_VALVE
        assert state.safety_valve_triggered is True

    def test_repeated_calls_after_safety_valve_stay_in_round_4(self) -> None:
        state = ProblemState()
        for _ in range(4):
            state.next_round()
        # Already at safety valve; further calls stay there
        r = state.next_round()
        assert r == Round.SAFETY_VALVE
        assert state.safety_valve_triggered is True


class TestTutoringAgentEndToEnd:
    """End-to-end tutoring flow."""

    async def test_first_round_uses_socratic(self, agent: TutoringAgent) -> None:
        output = await agent.ask("3 + 5 = ?", subtype="pure_calculation")
        assert output.metadata["round"] == 1
        assert output.metadata["safety_valve_triggered"] is False

    async def test_second_round_uses_analogy(self, agent: TutoringAgent) -> None:
        state = ProblemState()
        await agent.ask("3 + 5 = ?", subtype="pure_calculation", state=state)
        output = await agent.ask("3 + 5 = ?", subtype="pure_calculation", state=state)
        assert output.metadata["round"] == 2

    async def test_third_round_uses_visual(self, agent: TutoringAgent) -> None:
        state = ProblemState()
        await agent.ask("3 + 5 = ?", subtype="pure_calculation", state=state)
        await agent.ask("3 + 5 = ?", subtype="pure_calculation", state=state)
        output = await agent.ask("3 + 5 = ?", subtype="pure_calculation", state=state)
        assert output.metadata["round"] == 3

    async def test_safety_valve_on_fourth_attempt(self, agent: TutoringAgent) -> None:
        state = ProblemState()
        for _ in range(3):
            await agent.ask("3 + 5", subtype="pure_calculation", state=state)
        output = await agent.ask("3 + 5", subtype="pure_calculation", state=state)
        assert output.metadata["round"] == 4
        assert output.metadata["safety_valve_triggered"] is True

    async def test_never_give_direct_answer_in_guided_rounds(self, agent: TutoringAgent) -> None:
        """Even if the LLM accidentally reveals the answer, the agent
        replaces it with the guided-discovery template."""
        state = ProblemState()
        output = await agent.ask("3 + 5", subtype="pure_calculation", state=state)
        # Round 1: should NOT say "8" or "答案是"
        assert "答案是" not in output.text
        assert "等于 8" not in output.text or "等于" not in output.text  # careful match

    async def test_metadata_includes_difficulty(self, agent: TutoringAgent) -> None:
        output = await agent.ask("3 + 5", subtype="word_problem")
        assert "difficulty" in output.metadata
        assert output.metadata["difficulty"] in ("easy", "medium", "hard")

    async def test_metadata_includes_subtype(self, agent: TutoringAgent) -> None:
        output = await agent.ask("3 + 5", subtype="pure_calculation")
        assert output.metadata["subtype"] == "pure_calculation"

    async def test_empty_problem_returns_friendly_prompt(self, agent: TutoringAgent) -> None:
        output = await agent.ask("")
        assert "再说一次" in output.text or "没看清" in output.text


class TestTutoringAgentEmotionPreStep:
    """Emotional pre-step before tutoring (US #10)."""

    async def test_negative_emotion_triggers_pre_step(self, agent: TutoringAgent) -> None:
        output = await agent.ask("这道题我不会，太难了", subtype="pure_calculation")
        assert output.metadata["pre_step_applied"] is True
        # Pre-step template text should appear
        assert "不着急" in output.text or "Niki陪着你" in output.text

    async def test_neutral_emotion_no_pre_step(self, agent: TutoringAgent) -> None:
        output = await agent.ask("3 + 5 = ?", subtype="pure_calculation")
        assert output.metadata["pre_step_applied"] is False

    async def test_safety_valve_skips_pre_step(self, agent: TutoringAgent) -> None:
        """Even with negative emotion, the safety valve goes straight to answer."""
        state = ProblemState()
        for _ in range(3):
            await agent.ask("我不会太难了", subtype="pure_calculation", state=state)
        output = await agent.ask("我不会太难了", subtype="pure_calculation", state=state)
        assert output.metadata["safety_valve_triggered"] is True
        assert output.metadata["pre_step_applied"] is False


class TestTutoringAgentSafetyIntegration:
    """Output goes through the safety gateway (defense in depth)."""

    async def test_fear_word_rewrite_applied(self) -> None:
        class LLMWithFearWord:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"content": "怪物在计算数字"}

        agent = TutoringAgent(llm=LLMWithFearWord(), persona="gentle", child_age=7)
        output = await agent.ask("3 + 5", subtype="pure_calculation")
        assert "怪物" not in output.text

    async def test_direct_answer_in_guided_round_replaced(self) -> None:
        class LLMThatLeaksAnswer:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"content": "答案是8"}

        agent = TutoringAgent(llm=LLMThatLeaksAnswer(), persona="gentle", child_age=7)
        output = await agent.ask("3 + 5", subtype="pure_calculation")
        # Safety gateway catches direct answer → replaced with guided template
        assert "答案是8" not in output.text


class TestTutoringAgentZeroExternalTools:
    """PRD #54: agent must not have access to external tools."""

    def test_no_search_tool(self, agent: TutoringAgent) -> None:
        # Agent should not expose search / calculator
        assert not hasattr(agent, "search")
        assert not hasattr(agent, "calculate")

    def test_no_external_tool_imports(self) -> None:
        """Sanity check: no LLM-tool-bridges in tutoring.py."""
        import niki.agents.tutoring as mod
        # No tool definitions like @tool or tool definitions
        assert not hasattr(mod, "tools")
        assert not hasattr(mod, "calculator")
        assert not hasattr(mod, "search_api")


class TestStrategyTemplates:
    """All 4 rounds × 4 subtypes = 16 templates exist."""

    def test_all_rounds_present(self) -> None:
        for round in Round:
            assert round in STRATEGY_TEMPLATES

    def test_all_subtypes_per_round(self) -> None:
        for round in Round:
            templates = STRATEGY_TEMPLATES[round]
            for subtype in ("pure_calculation", "word_problem", "visual_problem", "open_ended"):
                assert subtype in templates, f"Missing {subtype} for round {round}"

    def test_difficulty_discount_per_subtype(self) -> None:
        assert "pure_calculation" in SUBTYPE_DIFFICULTY_DISCOUNT
        assert "word_problem" in SUBTYPE_DIFFICULTY_DISCOUNT
        assert "visual_problem" in SUBTYPE_DIFFICULTY_DISCOUNT
        assert "open_ended" in SUBTYPE_DIFFICULTY_DISCOUNT
        # Word problem has lower discount than pure calculation
        assert (
            SUBTYPE_DIFFICULTY_DISCOUNT["word_problem"]
            < SUBTYPE_DIFFICULTY_DISCOUNT["pure_calculation"]
        )
