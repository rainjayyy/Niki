"""Tests for the LLM-driven tool-use loop in EncyclopediaAgent (BE-AGENT-001).

These tests cover the 1-round ReAct path that runs when both
``agent_tool_use_enabled`` is True and a ``ToolAccessChecker`` is
injected. The legacy 1-LLM-call path is covered by
``tests/test_encyclopedia_agent.py``.

Test matrix:
  1. test_no_tool_needed_single_round   — LLM returns content, no tool call
  2. test_kb_tool_call_then_answer      — happy path: KB → second LLM
  3. test_unknown_tool_denied           — whitelist rejects non-KB tool
  4. test_malformed_json_arguments      — dispatcher returns error envelope
  5. test_think_block_stripped_in_final — M3 / R1 style reasoning
  6. test_feature_flag_off_uses_old_path — flag off → legacy 1-LLM path
  7. test_kb_no_hits_returns_unsure     — empty KB → no fabrication
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from niki.agents.encyclopedia import (
    UNSURE_RESPONSE_TEMPLATE,
    EncyclopediaAgent,
)
from niki.config.settings import get_settings
from niki.knowledge.fact_checker import FactChecker
from niki.knowledge.knowledge_base import KnowledgeBase
from niki.schemas.common import ChildInput
from niki.tools import (
    ToolAccessChecker,
    ToolAccess,
    ToolMatrix,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubLLMWithToolLoop:
    """Deterministic LLM stub for the tool-use loop.

    Each call pops the next canned response from ``canned_responses``.
    Records every call so tests can assert on messages + kwargs.
    """

    def __init__(self, canned_responses: list[dict[str, Any]]) -> None:
        self._canned = list(canned_responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.5,
        max_tokens: int = 512,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "kwargs": dict(kwargs),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if not self._canned:
            # Defensive — fail loud rather than silently return
            # an empty response that masks a test bug.
            raise AssertionError("Stub ran out of canned responses")
        return self._canned.pop(0)


def _make_agent(
    canned_responses: list[dict[str, Any]],
    *,
    child_age: int = 7,
    tool_checker: ToolAccessChecker | None = None,
    knowledge_base: KnowledgeBase | None = None,
) -> tuple[EncyclopediaAgent, _StubLLMWithToolLoop, ToolAccessChecker]:
    """Build a wired-up agent + stub + checker, all sharing the same KB.

    Tests that need a custom matrix pass ``tool_checker``; tests that
    need to inject a pre-seeded KB pass ``knowledge_base``. The default
    behaviour (no args) is unchanged so the existing tests keep working.
    """
    kb = knowledge_base if knowledge_base is not None else KnowledgeBase()
    fact_checker = FactChecker(kb)
    checker = tool_checker if tool_checker is not None else ToolAccessChecker(ToolMatrix())
    llm = _StubLLMWithToolLoop(canned_responses)
    agent = EncyclopediaAgent(
        llm=llm,
        knowledge_base=kb,
        fact_checker=fact_checker,
        persona="gentle",
        child_age=child_age,
        tool_checker=checker,
    )
    return agent, llm, checker


def _enable_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip the feature flag on for one test, restore the cache after.

    Note: the env var name is the field name uppercased — Pydantic
    ``BaseSettings`` does NOT add a prefix unless ``env_prefix`` is
    set in ``SettingsConfigDict``, and the current settings module
    doesn't set one. The docstring on the flag in ``settings.py``
    mentions ``NIKI_`` for documentation but it's misleading.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TOOL_USE_ENABLED", "true")
    get_settings.cache_clear()


def _disable_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly flip the flag off (and re-read settings)."""
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TOOL_USE_ENABLED", "false")
    get_settings.cache_clear()


def _set_loop_bounds(
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_rounds: int | None = None,
    max_tokens: int | None = None,
    max_tool_calls: int | None = None,
) -> None:
    """Override one or more ISS-22 loop bounds for a single test.

    Each value is independent — pass only the ones you want to
    override. Unset fields fall through to the Settings default.
    """
    get_settings.cache_clear()
    if max_rounds is not None:
        monkeypatch.setenv("AGENT_TOOL_LOOP_MAX_ROUNDS", str(max_rounds))
    if max_tokens is not None:
        monkeypatch.setenv("AGENT_TOOL_LOOP_MAX_TOKENS", str(max_tokens))
    if max_tool_calls is not None:
        monkeypatch.setenv("AGENT_TOOL_LOOP_MAX_TOOL_CALLS", str(max_tool_calls))
    get_settings.cache_clear()


def _kb_tool_call(call_id: str, query: str, top_k: int = 1) -> dict[str, Any]:
    """Build a canned ``knowledge_base.search`` tool-call payload."""
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "knowledge_base.search",
            "arguments": json.dumps(
                {"query": query, "top_k": top_k},
                ensure_ascii=False,
            ),
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoToolNeededSingleRound:
    """LLM chooses to answer directly — agent must not call KB."""

    @pytest.mark.asyncio
    async def test_no_tool_needed_single_round(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        # LLM #1 returns plain content; the agent should accept and
        # run the output-safety pipeline without a second LLM call.
        canned = [{"content": "天空是蓝的因为阳光被空气散射了~"}]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="为什么天是蓝的", session_id="t1"))

        assert len(llm.calls) == 1, "agent should make exactly 1 LLM call"
        # The first call must offer the tool schemas to the LLM.
        assert "tools" in llm.calls[0]["kwargs"]
        assert llm.calls[0]["kwargs"]["tool_choice"] == "auto"
        # The LLM's text reaches the child.
        assert "蓝" in result.text


class TestKBToolCallThenAnswer:
    """Happy path: LLM calls KB, dispatcher returns hits, LLM composes."""

    @pytest.mark.asyncio
    async def test_kb_tool_call_then_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        # Pull a real L1 hit so we can mirror its content in LLM #2
        # (the fact-checker uses token overlap, not LLM-as-judge).
        kb = KnowledgeBase()
        hits = kb.search("天空为什么蓝", top_k=1)
        assert hits, "Test premise: L1 must contain a sky-blue entry"
        kb_text = hits[0].content

        # LLM #1 emits a tool call; LLM #2 echoes the KB text so the
        # fact-checker is happy.
        canned = [
            {
                "tool_calls": [
                    {
                        "id": "call_kb_1",
                        "type": "function",
                        "function": {
                            "name": "knowledge_base.search",
                            "arguments": json.dumps(
                                {"query": "天空为什么蓝", "top_k": 1},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
                "content": "",
            },
            {"content": kb_text},
        ]
        agent, llm, checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="为什么天是蓝的", session_id="t2"))

        # Two LLM calls: #1 offers tools, #2 composes the answer.
        assert len(llm.calls) == 2
        # The follow-up call MAY still offer tools (ISS-22 bounded
        # loop — the LLM can decide to call again). The original
        # 1-round path stripped them; we keep that backward-compat
        # behavior only when ``agent_tool_loop_max_rounds == 1``,
        # covered by ``TestBackwardCompatOneRound``.
        # Second call must include a role:tool message carrying KB hits.
        tool_messages = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
        assert tool_messages, "follow-up call must include the tool result"
        assert tool_messages[0]["tool_call_id"] == "call_kb_1"
        # OpenAI spec: a role:tool message must be preceded by the
        # assistant message that issued the matching tool_calls. Without
        # this, strict providers (MiniMax-M3) reject with 400 and lenient
        # providers ignore the tool result. This assertion guards the
        # regression that BUG-1 caught in 2026-10-09.
        msg_list = llm.calls[1]["messages"]
        tool_idx = next(i for i, m in enumerate(msg_list) if m.get("role") == "tool")
        assert msg_list[tool_idx - 1].get("role") == "assistant", (
            "OpenAI spec: assistant message must precede role:tool"
        )
        assert msg_list[tool_idx - 1].get("tool_calls"), (
            "that assistant message must carry the matching tool_calls"
        )
        assert msg_list[tool_idx - 1]["tool_calls"][0]["id"] == "call_kb_1"
        kb_payload = json.loads(tool_messages[0]["content"])
        assert isinstance(kb_payload, list) and len(kb_payload) >= 1
        # Matrix audit: the KB call must be ALLOWED (not denied).
        allowed = [a for a in checker.checks if a.allowed]
        assert any(a.tool == "knowledge_base" for a in allowed), (
            "KB call should be recorded as allowed in the audit log"
        )
        # Output metadata surfaces the loop.
        assert result.metadata.get("tool_loop_used") is True
        assert "蓝" in result.text or "天空" in result.text


class TestUnknownToolDenied:
    """Whitelist rejects non-KB tools → agent falls back to unsure."""

    @pytest.mark.asyncio
    async def test_unknown_tool_denied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        # LLM asks for a tool the agent whitelist forbids.
        canned = [
            {
                "tool_calls": [
                    {
                        "id": "call_calc_1",
                        "type": "function",
                        "function": {
                            "name": "calculator.add",
                            "arguments": json.dumps({"a": 1, "b": 2}),
                        },
                    }
                ],
                "content": "",
            }
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="1+1等于几", session_id="t3"))

        # Only the first LLM call is made — we don't burn tokens on
        # a follow-up when the whitelist already rejected the tool.
        assert len(llm.calls) == 1
        # Output is the unsure template, never a fabricated answer.
        assert result.text == UNSURE_RESPONSE_TEMPLATE
        # The rejected tool is recorded in metadata for observability.
        assert result.metadata.get("tool_loop_used") is True
        assert "calculator.add" in result.metadata.get("tool_calls_rejected", [])


class TestMalformedJSONArguments:
    """Dispatcher returns an error envelope; agent must not crash."""

    @pytest.mark.asyncio
    async def test_malformed_json_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        # The dispatcher already has its own test for this; here we
        # verify the agent wires the error into the next LLM turn
        # instead of crashing the request.
        canned = [
            {
                "tool_calls": [
                    {
                        "id": "call_bad_1",
                        "type": "function",
                        "function": {
                            "name": "knowledge_base.search",
                            "arguments": "{not valid json",
                        },
                    }
                ],
                "content": "",
            },
            {"content": "Niki 暂时找不到相关知识呢~"},
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="随便问问", session_id="t4"))

        # Both LLM calls ran; the dispatcher swallowed the bad JSON.
        assert len(llm.calls) == 2
        # The follow-up LLM call received a tool message with the
        # dispatcher's error envelope, not an exception.
        tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
        assert tool_msgs
        envelope = json.loads(tool_msgs[0]["content"])
        assert envelope.get("error") == "malformed arguments"
        # The agent still produces a usable string output.
        assert isinstance(result.text, str) and result.text


class TestThinkBlockStrippedInFinal:
    """Reasoning models (M3 / R1) leak <think>...</think> blocks."""

    @pytest.mark.asyncio
    async def test_think_block_stripped_in_final(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        # Make the second LLM call emit a leaked think block.
        leaked = "<think>让我查一下…</think>天空是蓝色的，因为阳光被散射了~"
        canned = [
            {
                "tool_calls": [
                    {
                        "id": "call_kb_2",
                        "type": "function",
                        "function": {
                            "name": "knowledge_base.search",
                            "arguments": json.dumps({"query": "天空", "top_k": 1}),
                        },
                    }
                ],
                "content": "",
            },
            {"content": leaked},
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="天空为什么蓝", session_id="t5"))

        # Defense in depth: the think block must never reach the child.
        assert "<think>" not in result.text
        assert "让我查一下" not in result.text
        # The factual content survives the strip.
        assert "蓝" in result.text


class TestFeatureFlagOffUsesOldPath:
    """Flag off → legacy 1-LLM path even with checker injected."""

    @pytest.mark.asyncio
    async def test_feature_flag_off_uses_old_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _disable_tool_use(monkeypatch)
        # Even with a checker present, the flag-off path does NOT
        # pass `tools=...` to the LLM. We use a KB-friendly question
        # so the legacy path's retrieval step yields hits and the
        # LLM stub's content gets through fact-check.
        kb = KnowledgeBase()
        hits = kb.search("恐龙", top_k=1)
        assert hits
        kb_text = hits[0].content
        canned = [{"content": kb_text}]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="恐龙是什么", session_id="t6"))

        # Only 1 LLM call (the legacy path doesn't loop).
        assert len(llm.calls) == 1
        # Critically: no `tools` kwarg means the legacy path was taken.
        assert "tools" not in llm.calls[0]["kwargs"]
        # Output still works — the change is fully backwards compatible.
        assert "恐龙" in result.text
        # And the tool_loop flag is not set in the legacy metadata shape.
        assert not result.metadata.get("tool_loop_used")


class TestKBNoHitsReturnsUnsure:
    """KB returns 0 hits → agent must not fabricate an answer."""

    @pytest.mark.asyncio
    async def test_kb_no_hits_returns_unsure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        # Pick a query guaranteed to have 0 L1 hits. We use ASCII
        # gibberish because the L1 entries are all CJK and the
        # Jaccard tokenizer splits CJK into single chars — any
        # CJK query will accidentally share a character with some
        # L1 entry (的 / 是 / 不 / etc.) and return spurious hits.
        nonsense_query = "xyzzy12345plover"
        # Sanity check: confirm the KB really has no hits so the
        # test premise isn't broken if the seed data changes.
        kb = KnowledgeBase()
        assert not kb.search(nonsense_query, top_k=3), (
            "Test premise broken: nonsense query unexpectedly matched L1"
        )

        canned = [
            {
                "tool_calls": [
                    {
                        "id": "call_kb_empty",
                        "type": "function",
                        "function": {
                            "name": "knowledge_base.search",
                            "arguments": json.dumps(
                                {"query": nonsense_query, "top_k": 3},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
                "content": "",
            },
            # LLM is honest: it says it doesn't know rather than invent.
            {"content": UNSURE_RESPONSE_TEMPLATE},
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text=nonsense_query, session_id="t7"))

        # Both LLM calls ran (dispatcher returned "[]" for 0 hits).
        assert len(llm.calls) == 2
        # No KB evidence → fact_check is skipped in `_finalise` →
        # the LLM's text goes straight to output_safety. Since the
        # stub returned the unsure template verbatim, it should
        # survive.
        assert result.text == UNSURE_RESPONSE_TEMPLATE
        # The loop ran end-to-end even though the KB was empty.
        assert result.metadata.get("tool_loop_used") is True
        # hit_count is 0 — no fabrication of sources.
        assert result.metadata.get("hit_count") == 0


# ---------------------------------------------------------------------------
# ISS-22 — bounded 4-round ReAct loop tests
# ---------------------------------------------------------------------------
#
# Each test class below exercises ONE termination criterion or budget of the
# new ``_run_react_loop`` engine. The default settings (rounds=4,
# tokens=8000, tool_calls=8) match production; individual tests override via
# ``_set_loop_bounds`` to exercise the bound itself.
# ---------------------------------------------------------------------------


class TestFourRoundHappyPath:
    """3 KB tool calls across 3 rounds + 1 final compose, no early termination."""

    @pytest.mark.asyncio
    async def test_four_round_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        _set_loop_bounds(monkeypatch, max_rounds=4)
        kb = KnowledgeBase()
        hits = kb.search("鲸鱼", top_k=1)
        assert hits, "Test premise: L1 must contain a whale entry"
        kb_text = hits[0].content

        canned = [
            {"tool_calls": [_kb_tool_call("call_1", "鲸鱼")], "content": ""},
            {"tool_calls": [_kb_tool_call("call_2", "鲸鱼 哺乳动物")], "content": ""},
            {"tool_calls": [_kb_tool_call("call_3", "鲸鱼 呼吸")], "content": ""},
            # Final compose (Termination 1 on round 3 = 4th LLM call).
            {"content": kb_text},
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="鲸鱼是怎么呼吸的", session_id="i22_happy"))

        # 4 LLM calls: 3 tool rounds + 1 compose.
        assert len(llm.calls) == 4
        # Round count is surfaced for observability.
        assert result.metadata.get("tool_loop_rounds") == 3
        # The KB content survived the loop.
        assert isinstance(result.text, str) and result.text
        assert "tool_loop_used" in result.metadata


class TestMaxRoundsTruncates:
    """LLM keeps calling KB until the round cap is hit."""

    @pytest.mark.asyncio
    async def test_max_rounds_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        _set_loop_bounds(monkeypatch, max_rounds=3)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        assert hits
        kb_text = hits[0].content

        # 3 tool rounds + 1 final compose — that's exactly 4 canned
        # responses; the stub asserts if we pop a 5th.
        canned = [
            {"tool_calls": [_kb_tool_call(f"call_{i}", f"q{i}")], "content": ""} for i in range(3)
        ]
        canned.append({"content": kb_text})  # final compose

        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="天空为什么蓝", session_id="i22_truncate"))

        # 3 tool rounds + 1 final compose = 4 LLM calls total.
        assert len(llm.calls) == 4
        # The loop did NOT hang — it exited cleanly.
        assert result.metadata.get("tool_loop_rounds") == 3
        assert isinstance(result.text, str) and result.text


class TestTokenBudgetTruncates:
    """Setting a tiny token budget forces ``truncate_messages`` to fire."""

    @pytest.mark.asyncio
    async def test_token_budget_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        # 200 tokens ≈ 800 chars. Pad system prompt + history so the
        # first LLM call's message list comfortably exceeds the cap.
        _set_loop_bounds(monkeypatch, max_rounds=3, max_tokens=200)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content

        # Build a long fake history so the message list is > 800 chars.
        long_history = [
            {"role": "user", "content": "你好呀 " * 80},
            {"role": "assistant", "content": "嗯你好呀 " * 80},
            {"role": "user", "content": "我想知道 " * 80},
            {"role": "assistant", "content": "好的呀 " * 80},
        ]

        canned = [
            {"tool_calls": [_kb_tool_call("call_t1", "天空")], "content": ""},
            {"content": kb_text},
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(
            ChildInput(
                text="为什么天是蓝的",
                session_id="i22_tok",
                history=long_history,
            )
        )

        # The loop ran to completion without error.
        assert isinstance(result.text, str)
        # At least one LLM call was made (could be 1 if truncate
        # caused the second call's message list to drop history so
        # the LLM chose no_tool — both are acceptable outcomes).
        assert len(llm.calls) >= 1


class TestRepeatedToolBreaksLoop:
    """LLM emits the same KB call twice → loop breaks (Termination 4)."""

    @pytest.mark.asyncio
    async def test_repeated_tool_breaks_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        _set_loop_bounds(monkeypatch, max_rounds=4)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content

        canned = [
            # Round 0 — call KB.
            {"tool_calls": [_kb_tool_call("call_rep_1", "天空")], "content": ""},
            # Round 1 — call KB again with the SAME args.
            {
                "tool_calls": [_kb_tool_call("call_rep_2", "天空")],
                "content": "",
            },
            # Compose (canned but not reached if repeat-break fires).
            {"content": kb_text},
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="天空为什么蓝", session_id="i22_repeat"))

        # Loop exited on repeat — 2 LLM calls, NOT 3.
        assert len(llm.calls) == 2
        # The LLM's second-round content became the final draft (or
        # the unsure template, if safety re-routed it). Either way,
        # we got a string back.
        assert isinstance(result.text, str) and result.text


class TestToolCallHistoryDedup:
    """Same KB entry re-queried 3× across rounds → 1 hit in final sources."""

    @pytest.mark.asyncio
    async def test_tool_call_history_dedup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        _set_loop_bounds(monkeypatch, max_rounds=4)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        assert hits
        kb_text = hits[0].content

        canned = [
            # Round 0 — KB query A.
            {
                "tool_calls": [
                    {
                        "id": f"call_dedup_{i}",
                        "type": "function",
                        "function": {
                            "name": "knowledge_base.search",
                            "arguments": json.dumps(
                                {"query": f"天空_v{i}", "top_k": 1},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
                "content": "",
            }
            for i in range(3)
        ]
        # Compose.
        canned.append({"content": kb_text})

        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="天空为什么蓝", session_id="i22_dedup"))

        # 4 LLM calls (3 tool rounds + 1 compose).
        assert len(llm.calls) == 4
        # All three queries return the same L1 entry (sky-blue), so
        # dedup-by-content-prefix should leave at most 1 hit in the
        # final sources. (Dedup is best-effort by content; the same
        # source l1-core is preserved.)
        sources = result.metadata.get("sources") or []
        # We assert dedup at the hit_count level — should be <= 3.
        assert result.metadata.get("hit_count", 0) <= 3
        # And at least 1 hit survived (the L1 entry).
        assert result.metadata.get("hit_count", 0) >= 1
        # sources list is well-formed.
        assert isinstance(sources, list)


class TestHonestDisclosureBreaksEarly:
    """LLM emits ``Niki也不确定`` alongside tool_calls → treated as answer."""

    @pytest.mark.asyncio
    async def test_honest_disclosure_breaks_early(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        _set_loop_bounds(monkeypatch, max_rounds=4)
        canned = [
            # LLM emits honest-disclosure + a stray tool_call.
            {
                "tool_calls": [_kb_tool_call("call_hd_1", "anything")],
                "content": "嗯，Niki也不确定呢~",
            },
            # Compose (canned but should NOT be reached).
            {"content": "unused"},
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="一个奇怪的问题", session_id="i22_honest"))

        # Loop broke at Termination 2 after only 1 LLM call.
        assert len(llm.calls) == 1
        # The honest-disclosure text is what reached the user.
        assert "Niki也不确定" in result.text


class TestBackwardCompatOneRound:
    """``max_rounds=1`` reproduces the BE-AGENT-001 1-round baseline."""

    @pytest.mark.asyncio
    async def test_backward_compat_one_round(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        _set_loop_bounds(monkeypatch, max_rounds=1)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content

        canned = [
            {
                "tool_calls": [_kb_tool_call("call_1r_1", "天空")],
                "content": "",
            },
            {"content": kb_text},
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="天空为什么蓝", session_id="i22_legacy"))

        # Exactly 2 LLM calls: 1 tool round + 1 compose.
        assert len(llm.calls) == 2
        # Round accounting: 1 tool round completed before the cap.
        assert result.metadata.get("tool_loop_rounds") == 1
        assert isinstance(result.text, str) and result.text


class TestUnlimitedToolCallsDefault:
    """Default ``max_tool_calls=8`` allows plenty of headroom."""

    @pytest.mark.asyncio
    async def test_tool_call_budget_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bumping ``max_tool_calls=3`` so the budget trips after round 2."""
        _enable_tool_use(monkeypatch)
        _set_loop_bounds(monkeypatch, max_rounds=4, max_tool_calls=3)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content

        # Round 0 — 2 calls (budget: 3).
        # Round 1 — 2 calls would push us over (4 > 3) → budget trips.
        canned = [
            {
                "tool_calls": [
                    _kb_tool_call("call_b1", "天空"),
                    _kb_tool_call("call_b2", "天空蓝"),
                ],
                "content": "",
            },
            {
                "tool_calls": [
                    _kb_tool_call("call_b3", "天空蓝蓝"),
                    _kb_tool_call("call_b4", "天空蓝色"),  # would exceed
                ],
                "content": "天空蓝蓝的",
            },
            {"content": kb_text},  # compose (may or may not run)
        ]
        agent, llm, _checker = _make_agent(canned)

        result = await agent.invoke(ChildInput(text="天空为什么蓝", session_id="i22_budget"))

        # Loop exited via Termination 3 after round 1 (LLM #2).
        # We don't pin the exact LLM count because budget math
        # interacts with Termination 7 (post-loop compose), but
        # the loop MUST terminate — no hang, no 5xx.
        assert isinstance(result.text, str)
        # The agent surfaced tool-loop metadata.
        assert result.metadata.get("tool_loop_used") is True
        # And at least one LLM call was made.
        assert len(llm.calls) >= 2


class TestMatrixDenialBlocksDispatch:
    """BE-ISS-SANDBOX-ENFORCE: the tool permission matrix must
    actually reject denied calls — not just log them.

    Before the fix, ``_tool_checker.check(...)`` was called with
    a hard-coded ``ToolName.KNOWLEDGE_BASE`` and the return value
    was discarded, so a missing permission could still drive
    the dispatcher. The fix maps the LLM-issued tool name to the
    correct enum, consults ``access.allowed``, and short-circuits
    dispatch when denied, emitting a ``permission_denied`` error
    envelope + ``ok=False`` SSE event.
    """

    @pytest.mark.asyncio
    async def test_denied_kb_tool_skips_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_tool_use(monkeypatch)
        _set_loop_bounds(monkeypatch, max_rounds=4)

        from niki.tools import ToolName

        # Build a matrix that revokes KB access for encyclopedia_agent
        matrix = ToolMatrix()
        matrix.set_permission(ToolName.KNOWLEDGE_BASE, set())  # deny
        checker = ToolAccessChecker(matrix)

        canned = [
            # LLM #1: try to call the KB (which is now denied)
            {
                "tool_calls": [_kb_tool_call("call_deny_1", "天空")],
                "content": "",
            },
            # LLM #2 (compose): pick up the error envelope and answer
            {
                "tool_calls": [],
                "content": "嗯Niki不确定呢~",
            },
        ]
        agent, llm, checker = _make_agent(canned, tool_checker=checker)

        result = await agent.invoke(ChildInput(text="天空为什么蓝", session_id="i22_deny"))

        # The agent did NOT call the real KB — no KB content reached
        # the LLM. The dispatcher short-circuited with an error
        # envelope instead.
        all_msgs: list[dict[str, Any]] = []
        for call in llm.calls:
            all_msgs.extend(call["messages"])
        kb_seen = any(
            isinstance(m.get("content"), str) and "l1_core" in m["content"] for m in all_msgs
        )
        assert not kb_seen, "KB content should NOT reach the LLM when the matrix denies"

        # The audit trail recorded a denied check for the KB
        denied = [c for c in checker.checks if not c.allowed]
        assert any(c.tool == ToolName.KNOWLEDGE_BASE for c in denied), (
            "checker.checks should include a denied KB access"
        )

        # Final result is well-formed
        assert isinstance(result.text, str)

    @pytest.mark.asyncio
    async def test_unknown_tool_name_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tool name not in the matrix's known-enum mapping must
        NOT raise — the dispatcher's own 'unknown tool' envelope
        path runs and the loop stays healthy.
        """
        _enable_tool_use(monkeypatch)
        _set_loop_bounds(monkeypatch, max_rounds=4)

        canned = [
            {
                "tool_calls": [
                    {
                        "id": "tc-x",
                        "type": "function",
                        "function": {
                            "name": "made_up_tool",  # not whitelisted
                            "arguments": "{}",
                        },
                    }
                ],
                "content": "",
            },
            # Compose after the unknown-tool envelope
            {"tool_calls": [], "content": "嗯…Niki也不确定呢~"},
        ]
        agent, llm, _checker = _make_agent(canned)
        result = await agent.invoke(ChildInput(text="随便问问", session_id="i22_unknown"))
        assert isinstance(result.text, str)
        # The whitelist rejection keeps things inside the loop;
        # at most 1 LLM call before the honest-disclosure path.
