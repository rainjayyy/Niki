"""Chat context integration: ShortTermMemory feeds dialogue history."""

import pytest

from niki.memory.short_term import (
    ConversationTurn,
    format_history,
    turns_to_history,
)


def test_turns_to_history_maps_roles() -> None:
    turns = [
        ConversationTurn(role="child", text="你好"),
        ConversationTurn(role="niki", text="你好呀"),
    ]
    assert turns_to_history(turns) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ]


def test_format_history_skips_invalid_entries() -> None:
    assert format_history(
        [
            {"role": "user", "content": " 你好 "},
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": ""},
            {"role": "assistant", "content": " 收到 "},
        ]
    ) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "收到"},
    ]


class _RecordingLLM:
    provider_name = "Minimax"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN002
        self.calls.append(messages)
        return {"content": "你好，我是Niki~"}


@pytest.mark.asyncio
async def test_orchestrator_remembers_context() -> None:
    """Turn 2's LLM call should include turn 1's child text + Niki's reply.

    Background: the original version of this test routed both turns
    through the emotion path, but the emotion agent in v1 uses a
    curated template (no LLM call) — see niki/agents/emotion.py:
    _build_response. We now use encyclopedia-keyword inputs that the
    rule-based classifier routes to the encyclopedia agent, which
    DOES call the LLM on every turn. This exercises the actual
    short-term-memory → ChildInput.history → LLM.messages pipeline.

    "为什么" is in the rule-based classifier's _ENCYCLOPEDIA_KEYWORDS
    so both turns route to encyclopedia without invoking the LLM-based
    classifier path.
    """
    from niki.agents.orchestrator import ChatOrchestrator

    llm = _RecordingLLM()
    orchestrator = ChatOrchestrator(child_age=7, persona="gentle", llm=llm)

    # Turn 1: routes to encyclopedia (why-keyword) → LLM called →
    # returns "你好，我是Niki~" → orchestrator appends both turns to
    # short-term memory.
    await orchestrator.chat("为什么天空是蓝的？", session_id="ctx-1")
    # Turn 2: also routes to encyclopedia. The orchestrator reads
    # turn 1 from short-term memory and forwards it to the LLM as
    # the message history of the encyclopedia agent's invoke() call.
    await orchestrator.chat("刚才问的是什么？", session_id="ctx-1")

    assert llm.calls, "expected at least one LLM call across the two turns"
    last_messages = llm.calls[-1]
    user_contents = " ".join(m["content"] for m in last_messages if m.get("role") == "user")
    assistant_contents = " ".join(
        m["content"] for m in last_messages if m.get("role") == "assistant"
    )
    # Turn 1's child text was forwarded to the LLM as part of the
    # history on turn 2.
    assert "为什么天空是蓝的" in user_contents
    # Turn 1's Niki reply was forwarded to the LLM as an assistant
    # turn in the history.
    assert "你好，我是Niki~" in assistant_contents
    # Turn 2's own child text is the final user message.
    assert "刚才问的是什么" in user_contents


class _CtxLLM:
    provider_name = "Minimax"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN002
        self.calls.append(messages)
        return {"content": "你刚才问的是恐龙。"}


@pytest.mark.asyncio
async def test_encyclopedia_answers_from_history_without_kb() -> None:
    """Follow-up questions can be answered from history even with no KB hit."""
    from niki.agents.encyclopedia import EncyclopediaAgent
    from niki.knowledge.fact_checker import FactChecker
    from niki.knowledge.knowledge_base import KnowledgeBase
    from niki.schemas.common import ChildInput

    kb = KnowledgeBase(l1_entries=[], l2_entries=[])
    llm = _CtxLLM()
    agent = EncyclopediaAgent(
        llm=llm,
        knowledge_base=kb,
        fact_checker=FactChecker(kb),
        persona="gentle",
        child_age=7,
    )
    out = await agent.invoke(
        ChildInput(
            text="我刚才问的是什么？",
            session_id="enc-ctx",
            history=[
                {"role": "user", "content": "恐龙为什么会灭绝？"},
                {"role": "assistant", "content": "因为地球变冷了。"},
            ],
        )
    )
    assert out.text == "你刚才问的是恐龙。"
    last = llm.calls[-1]
    user_contents = " ".join(m["content"] for m in last if m.get("role") == "user")
    assert "恐龙为什么会灭绝" in user_contents
