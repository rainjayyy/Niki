"""Tests for ISS-23 ChatOrchestrator.stream_chat() structured SSE events.

The orchestrator's streaming path runs the full pipeline (input
safety → classify → route → dispatch with tool loop → fuse → output
safety) and yields dicts with a discriminator key (``kind``):

  * ``{"kind": "phase", ...}`` — well-defined transitions.
  * ``{"kind": "chunk", "text": str, "token_index": int}`` — final
    answer codepoint chunks (after output safety).
  * ``{"kind": "tool_call", ...}`` — LLM-initiated tool calls.
  * ``{"kind": "tool_result", ...}`` — dispatcher results.
  * ``{"kind": "final", "is_final": True, ...}`` — terminal event.

Each test consumes the async generator with ``[evt async for evt in
orchestrator.stream_chat(...)]`` and asserts on the event ordering,
discriminator keys, and payload shapes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from niki.agents.encyclopedia import EncyclopediaAgent
from niki.agents.orchestrator import ChatOrchestrator
from niki.config.settings import get_settings
from niki.knowledge.fact_checker import FactChecker
from niki.knowledge.knowledge_base import KnowledgeBase
from niki.schemas.common import ChildInput
from niki.tools import ToolAccessChecker, ToolMatrix


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubLLMForStream:
    """Replies with KB tool_calls then a content answer. Records all calls."""

    def __init__(self, canned: list[dict[str, Any]]) -> None:
        self._canned = list(canned)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, temperature=0.5, max_tokens=512, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        if not self._canned:
            raise AssertionError("Stub ran out of canned responses")
        return self._canned.pop(0)


def _make_orchestrator(
    llm_canned: list[dict[str, Any]],
) -> tuple[ChatOrchestrator, _StubLLMForStream]:
    """Build an orchestrator wired to the streaming stub LLM."""
    llm = _StubLLMForStream(llm_canned)
    orch = ChatOrchestrator(child_age=7, persona="gentle", llm=llm)
    return orch, llm


def _enable_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TOOL_USE_ENABLED", "true")
    get_settings.cache_clear()


def _disable_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TOOL_USE_ENABLED", "false")
    get_settings.cache_clear()


async def _collect(stream) -> list[dict[str, Any]]:
    """Drain an async generator into a list."""
    out: list[dict[str, Any]] = []
    async for evt in stream:
        out.append(evt)
    return out


def _kb_tool_call(call_id: str, query: str, top_k: int = 1) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "knowledge_base.search",
            "arguments": json.dumps(
                {"query": query, "top_k": top_k}, ensure_ascii=False
            ),
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStreamHappyPath:
    """Happy path — input ok, intent routed, encyclopedia agent runs KB loop."""

    @pytest.mark.asyncio
    async def test_stream_happy_path_phases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_tool_use(monkeypatch)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        assert hits
        kb_text = hits[0].content

        canned = [
            {"tool_calls": [_kb_tool_call("call_s1", "天空")], "content": ""},
            {"content": kb_text},
        ]
        orch, _llm = _make_orchestrator(canned)

        events = await _collect(
            orch.stream_chat(
                "天空为什么蓝", session_id="i23_happy", child_age=7
            )
        )

        # Phase events present in expected order.
        phase_names = [
            e["name"]
            for e in events
            if e.get("kind") == "phase"
        ]
        assert "input_safety_ok" in phase_names
        assert "routed" in phase_names
        assert "agent_start" in phase_names
        assert "agent_end" in phase_names
        assert "fused" in phase_names
        assert "output_safety_ok" in phase_names

        # tool_call / tool_result events present.
        kinds = [e.get("kind") for e in events]
        assert "tool_call" in kinds
        assert "tool_result" in kinds

        # At least one chunk event.
        chunks = [e for e in events if e.get("kind") == "chunk"]
        assert chunks, "Expected at least one chunk event"

        # Final event with is_final=True.
        final = next((e for e in events if e.get("kind") == "final"), None)
        assert final is not None
        assert final.get("is_final") is True
        assert "total_text" in final


class TestStreamToolCallPayloadShape:
    """tool_call / tool_result payloads carry the closed-enum shape."""

    @pytest.mark.asyncio
    async def test_tool_call_event_payload_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_tool_use(monkeypatch)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content

        canned = [
            {"tool_calls": [_kb_tool_call("call_shape", "天空")], "content": ""},
            {"content": kb_text},
        ]
        orch, _llm = _make_orchestrator(canned)

        events = await _collect(
            orch.stream_chat(
                "天空为什么蓝", session_id="i23_shape", child_age=7
            )
        )

        tool_call_evts = [e for e in events if e.get("kind") == "tool_call"]
        assert tool_call_evts, "expected at least one tool_call event"
        tc = tool_call_evts[0]
        assert tc["agent"] == "encyclopedia"
        assert tc["name"] == "knowledge_base.search"
        assert isinstance(tc["args"], dict)
        assert "query" in tc["args"]

        tool_result_evts = [
            e for e in events if e.get("kind") == "tool_result"
        ]
        assert tool_result_evts
        tr = tool_result_evts[0]
        assert tr["agent"] == "encyclopedia"
        assert tr["name"] == "knowledge_base.search"
        assert tr["ok"] is True
        assert isinstance(tr["hit_count"], int)


class TestStreamTokenIndexMonotonic:
    """All chunk token_index values strictly increase."""

    @pytest.mark.asyncio
    async def test_stream_token_index_monotonic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_tool_use(monkeypatch)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content

        canned = [
            {"tool_calls": [_kb_tool_call("call_mono", "天空")], "content": ""},
            {"content": kb_text},
        ]
        orch, _llm = _make_orchestrator(canned)

        events = await _collect(
            orch.stream_chat(
                "天空为什么蓝", session_id="i23_mono", child_age=7
            )
        )

        chunk_evts = [e for e in events if e.get("kind") == "chunk"]
        assert len(chunk_evts) >= 2
        for prev, cur in zip(chunk_evts, chunk_evts[1:]):
            assert cur["token_index"] > prev["token_index"]


class TestStreamParallelAgentDiscriminator:
    """Every parallel-agent phase event carries an ``agent: str`` discriminator."""

    @pytest.mark.asyncio
    async def test_agent_phase_events_carry_agent_discriminator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_tool_use(monkeypatch)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content

        canned = [
            {"tool_calls": [_kb_tool_call("call_disc", "天空")], "content": ""},
            {"content": kb_text},
        ]
        orch, _llm = _make_orchestrator(canned)

        events = await _collect(
            orch.stream_chat(
                "天空为什么蓝", session_id="i23_disc", child_age=7
            )
        )

        agent_starts = [
            e for e in events
            if e.get("kind") == "phase" and e.get("name") == "agent_start"
        ]
        assert agent_starts
        for a in agent_starts:
            assert "agent" in a.get("payload", {})
            assert isinstance(a["payload"]["agent"], str)


class TestStreamCallbackExceptionDoesNotCrashAgent:
    """A misbehaving on_chunk must NOT bubble up; agent returns its output."""

    @pytest.mark.asyncio
    async def test_callback_exception_isolated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_tool_use(monkeypatch)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content

        canned = [
            {"tool_calls": [_kb_tool_call("call_cb1", "天空")], "content": ""},
            {"content": kb_text},
        ]
        orch, _llm = _make_orchestrator(canned)

        # Inject a phase callback that always raises. The agent's
        # defensive wrapper should swallow it; the final event still
        # arrives.
        from niki.agents.encyclopedia import EncyclopediaAgent

        original_invoke = EncyclopediaAgent.invoke

        async def wrapped(self, child_input, context=None, *, on_phase=None, on_chunk=None):
            async def boom(name, payload):
                raise RuntimeError("simulated consumer crash")

            return await original_invoke(
                self,
                child_input,
                context,
                on_phase=boom if on_phase is not None else None,
                on_chunk=on_chunk,
            )

        monkeypatch.setattr(EncyclopediaAgent, "invoke", wrapped)

        events = await _collect(
            orch.stream_chat(
                "天空为什么蓝", session_id="i23_cb", child_age=7
            )
        )

        # Final event still arrived — agent didn't crash.
        final = next((e for e in events if e.get("kind") == "final"), None)
        assert final is not None
        assert final.get("is_final") is True


class TestStreamInputBlocked:
    """Input blocked → short-circuits with input_blocked + final."""

    @pytest.mark.asyncio
    async def test_stream_input_blocked_short_circuit(self) -> None:
        orch, _llm = _make_orchestrator([])

        events = await _collect(
            orch.stream_chat(
                "我想杀了他", session_id="i23_block", child_age=9
            )
        )

        kinds = [e.get("kind") for e in events]
        # No agent events when input is blocked.
        assert "tool_call" not in kinds
        assert "tool_result" not in kinds

        phase_names = [
            e["name"] for e in events if e.get("kind") == "phase"
        ]
        assert "input_blocked" in phase_names

        final = next((e for e in events if e.get("kind") == "final"), None)
        assert final is not None
        assert final.get("safety_alert") is True


class TestStreamInputEscalation:
    """Self-harm → input_escalation + caring_text chunk + parent_alert_required."""

    @pytest.mark.asyncio
    async def test_stream_input_escalation(self) -> None:
        orch, _llm = _make_orchestrator([])

        events = await _collect(
            orch.stream_chat(
                "我不想活了", session_id="i23_escalate", child_age=9
            )
        )

        phase_names = [
            e["name"] for e in events if e.get("kind") == "phase"
        ]
        assert "input_escalation" in phase_names

        chunks = [e for e in events if e.get("kind") == "chunk"]
        assert chunks

        final = next((e for e in events if e.get("kind") == "final"), None)
        assert final is not None
        assert final.get("parent_alert_required") is True
        assert final.get("safety_alert") is True


class TestStreamFlagOffUsesRawProvider:
    """When agent_stream_use_agent_pipeline=False, /chat/stream uses raw path.

    This is structurally verified by checking the orchestrator's
    stream_chat() method exists and yields events regardless of the
    flag — the FLAG ONLY controls whether ``chat_stream`` wires it.
    We assert the orchestrator itself is flag-independent.
    """

    @pytest.mark.asyncio
    async def test_orchestrator_stream_chat_always_emits_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Flag-off at the settings layer; the orchestrator's
        # stream_chat still emits events because the flag is only
        # consulted by the SSE endpoint, not by the orchestrator.
        get_settings.cache_clear()
        monkeypatch.setenv("AGENT_STREAM_USE_AGENT_PIPELINE", "false")
        get_settings.cache_clear()
        _enable_tool_use(monkeypatch)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content
        canned = [
            {"tool_calls": [_kb_tool_call("call_raw", "天空")], "content": ""},
            {"content": kb_text},
        ]
        orch, _llm = _make_orchestrator(canned)
        events = await _collect(
            orch.stream_chat(
                "天空为什么蓝", session_id="i23_raw", child_age=7
            )
        )
        # The orchestrator emits events regardless of the flag.
        final = next((e for e in events if e.get("kind") == "final"), None)
        assert final is not None


class TestStreamFinalPayloadShape:
    """The ``final`` event payload matches the SSE wire contract."""

    @pytest.mark.asyncio
    async def test_final_payload_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_tool_use(monkeypatch)
        kb = KnowledgeBase()
        hits = kb.search("天空", top_k=1)
        kb_text = hits[0].content
        canned = [
            {"tool_calls": [_kb_tool_call("call_fin", "天空")], "content": ""},
            {"content": kb_text},
        ]
        orch, _llm = _make_orchestrator(canned)

        events = await _collect(
            orch.stream_chat(
                "天空为什么蓝", session_id="i23_final", child_age=7
            )
        )

        final = next((e for e in events if e.get("kind") == "final"), None)
        assert final is not None
        # Backward-compat keys (the raw path emitted these too).
        assert "session_id" in final
        assert "total_text" in final
        assert "is_final" in final
        # ISS-23 additions.
        assert "intent" in final
        assert "primary_agent" in final
        assert "stages_completed" in final
        assert "metadata" in final
        assert "safety_alert" in final
        assert "parent_alert_required" in final
