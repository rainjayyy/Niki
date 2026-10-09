"""Unit tests for niki.tools.dispatcher (BE-AGENT-001)."""

from __future__ import annotations

import json

import pytest

from niki.knowledge.knowledge_base import KnowledgeBase
from niki.tools.dispatcher import dispatch_tool_call


def _call(name: str, arguments: object) -> dict:
    """Build a minimal OpenAI tool_call dict for tests."""
    if isinstance(arguments, dict):
        args_str = json.dumps(arguments, ensure_ascii=False)
    else:
        args_str = str(arguments)
    return {
        "id": "call_test_001",
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }


class TestKnowledgeBaseTool:
    @pytest.mark.asyncio
    async def test_returns_kb_hits(self) -> None:
        kb = KnowledgeBase()
        call = _call("knowledge_base.search", {"query": "天空为什么蓝", "top_k": 2})
        result = await dispatch_tool_call(call, kb=kb)
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        # The L1 has a sky-blue entry that should match.
        assert any("蓝" in item.get("content", "") for item in parsed)
        # Schema-level clamp: every hit has the expected keys.
        for item in parsed:
            assert {"id", "source", "score", "content"} <= item.keys()

    @pytest.mark.asyncio
    async def test_empty_query_returns_error_envelope(self) -> None:
        kb = KnowledgeBase()
        call = _call("knowledge_base.search", {"query": "  ", "top_k": 3})
        result = await dispatch_tool_call(call, kb=kb)
        parsed = json.loads(result)
        assert "error" in parsed
        assert parsed["error"] == "empty query"


class TestMalformedArguments:
    @pytest.mark.asyncio
    async def test_broken_json_returns_error_envelope(self) -> None:
        kb = KnowledgeBase()
        call = {
            "id": "call_bad",
            "type": "function",
            "function": {"name": "knowledge_base.search", "arguments": "{not valid json"},
        }
        result = await dispatch_tool_call(call, kb=kb)
        parsed = json.loads(result)
        assert parsed["error"] == "malformed arguments"
        assert parsed["tool"] == "knowledge_base.search"


class TestUnknownTool:
    @pytest.mark.asyncio
    async def test_unknown_tool_name_returns_error(self) -> None:
        kb = KnowledgeBase()
        call = _call("calculator.add", {"a": 1, "b": 2})
        result = await dispatch_tool_call(call, kb=kb)
        parsed = json.loads(result)
        assert parsed["error"] == "unknown tool"
        assert parsed["tool"] == "calculator.add"


class TestTimeQuery:
    @pytest.mark.asyncio
    async def test_returns_iso_timestamp(self) -> None:
        kb = KnowledgeBase()
        call = _call("time_query.now", {})
        result = await dispatch_tool_call(call, kb=kb)
        parsed = json.loads(result)
        assert "now_utc" in parsed
        # ISO 8601 with timezone marker.
        assert "T" in parsed["now_utc"]
        assert parsed["now_utc"].endswith("+00:00") or parsed["now_utc"].endswith("Z")


class TestImageRecognition:
    @pytest.mark.asyncio
    async def test_with_caption(self) -> None:
        kb = KnowledgeBase()
        call = _call("image_recognition.caption", {"image_url": "https://x/y.png"})
        result = await dispatch_tool_call(
            call, kb=kb, image_caption="一只橘猫在睡觉"
        )
        parsed = json.loads(result)
        assert parsed["caption"] == "一只橘猫在睡觉"

    @pytest.mark.asyncio
    async def test_without_caption_uses_fallback(self) -> None:
        kb = KnowledgeBase()
        call = _call("image_recognition.caption", {"image_url": "https://x/y.png"})
        result = await dispatch_tool_call(call, kb=kb)
        parsed = json.loads(result)
        # Fallback Chinese string — the actual wording is the
        # dispatcher's contract.
        assert "caption" in parsed
        assert len(parsed["caption"]) > 0
