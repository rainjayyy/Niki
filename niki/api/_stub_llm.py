"""Shared stub LLM for development + tests.

ISS-11: replaces 4 duplicate StubLLM classes scattered across the API
modules. The shared stub is deterministic and safe — it never returns
content that would bypass safety checks.
"""

from __future__ import annotations

from typing import Any


class _StubLLM:
    """Deterministic stub LLM used by the API modules.

    Returns canned content that mimics the LLM's output for testing
    + local development. The stub is intentionally boring — no real
    network calls, no real model, no surprises.

    Per-agent subclasses (below) customise the response.
    """

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 512,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Default: just echo a safe phrase
        return {
            "content": "嗯~ Niki在听你说呢~",
            "model": "stub-llm",
            "usage": {"total_tokens": 0},
            "finish_reason": "stop",
        }


class _EncyclopediaStub(_StubLLM):
    """Stub for encyclopedia: returns the first KB hit content."""

    async def chat(self, messages, temperature=0.7, max_tokens=512, **kwargs):
        user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "",
        )
        ref_block = user_msg.split("参考资料：", 1)[1] if "参考资料：" in user_msg else user_msg
        if ref_block.strip():
            return {
                "content": ref_block.strip(),
                "model": "stub-encyclopedia",
                "usage": {},
                "finish_reason": "stop",
            }
        return {
            "content": "嗯，Niki对这个也不太确定呢~",
            "model": "stub-encyclopedia",
            "usage": {},
            "finish_reason": "stop",
        }


class _TutoringStub(_StubLLM):
    """Stub for tutoring: returns the guided-discovery prompt as-is."""

    async def chat(self, messages, temperature=0.6, max_tokens=256, **kwargs):
        user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "",
        )
        if "「" in user_msg and "」" in user_msg:
            prompt = user_msg.split("「", 1)[1].split("」", 1)[0]
        else:
            prompt = "我们一起看看这道题~"
        return {
            "content": prompt,
            "model": "stub-tutoring",
            "usage": {},
            "finish_reason": "stop",
        }
