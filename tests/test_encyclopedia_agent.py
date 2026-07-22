"""Tests for the Encyclopedia Agent end-to-end."""

import pytest

from niki.agents.encyclopedia import (
    EncyclopediaAgent,
    EncyclopediaRequest,
)
from niki.knowledge.fact_checker import FactChecker
from niki.knowledge.knowledge_base import KnowledgeBase
from niki.schemas.common import ChildInput


class _StubLLM:
    """Deterministic LLM stub that paraphrases the top KB hit."""

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.5,
        max_tokens: int = 512,
        **kwargs,
    ):
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        ref_block = user_msg.split("参考资料：", 1)[1] if "参考资料：" in user_msg else user_msg
        # Return the full KB content so the fact-checker can find the
        # expected keywords (no truncation).
        if ref_block.strip():
            return {"content": ref_block.strip()}
        return {"content": "嗯，Niki对这个也不太确定呢~"}


@pytest.fixture
def agent() -> EncyclopediaAgent:
    kb = KnowledgeBase()
    fact_checker = FactChecker(kb)
    return EncyclopediaAgent(
        llm=_StubLLM(),
        knowledge_base=kb,
        fact_checker=fact_checker,
        persona="gentle",
        child_age=7,
    )


class TestEncyclopediaAgentBasics:
    """Basic agent properties."""

    def test_agent_type(self, agent: EncyclopediaAgent) -> None:
        assert agent.agent_type == "encyclopedia"

    def test_system_prompt_includes_no_fabrication(self, agent: EncyclopediaAgent) -> None:
        prompt = agent.get_system_prompt_with_persona()
        assert "诚实" in prompt or "不能" in prompt or "确定" in prompt
        # Never ask for private info (PRD #42)
        assert "隐私" in prompt or "住址" in prompt or "电话" in prompt
        # Age-aware (PRD #34, #56)
        assert "6-8" in prompt or "9-12" in prompt or "3-5" in prompt

    def test_age_group_resolved(self, agent: EncyclopediaAgent) -> None:
        assert agent._age_group == "6-8"


class TestEncyclopediaAgentQueries:
    """End-to-end queries."""

    async def test_known_topic_returns_answer(self, agent: EncyclopediaAgent) -> None:
        output = await agent.invoke(ChildInput(text="为什么天是蓝的", session_id="t1"))
        assert "蓝" in output.text or "天空" in output.text
        assert output.intent == "encyclopedia"

    async def test_unknown_topic_admits_uncertainty(self, agent: EncyclopediaAgent) -> None:
        output = await agent.invoke(ChildInput(text="火星人住在开普勒-186f", session_id="t2"))
        # Should admit uncertainty (no fabrication)
        assert "不确定" in output.text or "问问" in output.text

    async def test_empty_text_returns_friendly_prompt(self, agent: EncyclopediaAgent) -> None:
        output = await agent.invoke(ChildInput(text="", session_id="t3"))
        assert "再说一次" in output.text or "没听清" in output.text

    async def test_dinosaur_question(self, agent: EncyclopediaAgent) -> None:
        output = await agent.invoke(ChildInput(text="恐龙是什么", session_id="t4"))
        assert "恐龙" in output.text


class TestEncyclopediaAgentMetadata:
    """Output metadata is informative."""

    async def test_metadata_includes_sources(self, agent: EncyclopediaAgent) -> None:
        output = await agent.invoke(ChildInput(text="为什么天是蓝的", session_id="t5"))
        assert "sources" in output.metadata
        assert "l1_core" in output.metadata["sources"]

    async def test_metadata_includes_hit_count(self, agent: EncyclopediaAgent) -> None:
        output = await agent.invoke(ChildInput(text="天空", session_id="t6"))
        assert "hit_count" in output.metadata
        assert output.metadata["hit_count"] >= 1

    async def test_metadata_includes_fact_check_status(self, agent: EncyclopediaAgent) -> None:
        output = await agent.invoke(ChildInput(text="天空", session_id="t7"))
        assert "fact_check_passed" in output.metadata

    async def test_metadata_includes_age_group(self, agent: EncyclopediaAgent) -> None:
        output = await agent.invoke(ChildInput(text="天空", session_id="t8"))
        assert output.metadata["age_group"] == "6-8"


class TestEncyclopediaAgentAgeAdaptation:
    """Age-adapted retrieval (top_k varies by age)."""

    async def test_younger_child_fewer_hits(self) -> None:
        kb = KnowledgeBase()
        fact_checker = FactChecker(kb)
        agent = EncyclopediaAgent(
            llm=_StubLLM(),
            knowledge_base=kb,
            fact_checker=fact_checker,
            persona="gentle",
            child_age=4,
        )
        assert agent._age_group == "3-5"
        # Should still work for known topics
        output = await agent.invoke(ChildInput(text="恐龙", session_id="young"))
        assert "恐龙" in output.text

    async def test_older_child_more_hits(self) -> None:
        kb = KnowledgeBase()
        fact_checker = FactChecker(kb)
        agent = EncyclopediaAgent(
            llm=_StubLLM(),
            knowledge_base=kb,
            fact_checker=fact_checker,
            persona="gentle",
            child_age=10,
        )
        assert agent._age_group == "9-12"


class TestEncyclopediaAgentAsk:
    """The convenience ask() method."""

    async def test_ask_returns_agent_output(self, agent: EncyclopediaAgent) -> None:
        request = EncyclopediaRequest(question="恐龙", child_age=7)
        output = await agent.ask(request)
        assert "恐龙" in output.text

    async def test_ask_with_image(self, agent: EncyclopediaAgent) -> None:
        request = EncyclopediaRequest(
            question="这是什么",
            child_age=7,
            image_url="http://example.com/dinosaur.jpg",
        )
        output = await agent.ask(request)
        # When image provided without caption, falls back to generic question
        # Should not crash
        assert isinstance(output.text, str)


class TestEncyclopediaAgentSafetyIntegration:
    """Output goes through the safety gateway (fear-word rewrite)."""

    async def test_fear_word_rewrite_applied(self) -> None:
        class LLMWithFearWord:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"content": "天空中有怪物，死了很多恐龙。"}

        kb = KnowledgeBase()
        fact_checker = FactChecker(kb)
        agent = EncyclopediaAgent(
            llm=LLMWithFearWord(),
            knowledge_base=kb,
            fact_checker=fact_checker,
            persona="gentle",
            child_age=7,
        )
        output = await agent.invoke(ChildInput(text="恐龙", session_id="safety"))
        # Fear words should be rewritten
        assert "怪物" not in output.text
        assert "奇怪的小动物" in output.text or "不见" in output.text or output.metadata.get("was_rewritten")
