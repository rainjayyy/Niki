"""Streaming LLM provider abstraction (BE-RT-001).

The contract:
    `stream(prompt, **kwargs)` is an async generator that yields
    successive text tokens. Callers wrap the stream in SSE or
    WebSocket responses.

Implementations:
    - `FallbackStreamingProvider` — default; deterministic word-by-word
      emission so tests are reproducible and demos feel real.
    - `StreamingLLMProvider`  — abstract base; production plug-in
                                point for DeepSeek-V4 / doubao
                                streaming mode (BE-RT-001-real).
"""

from __future__ import annotations
from typing import Any

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class StreamingLLMProvider(ABC):
    """Contract for a streaming LLM.

    Implementations yield non-empty chunks; the end of stream is
    signalled by the generator's natural StopIteration.
    """

    @abstractmethod
    async def stream(
        self,
        prompt: str,
        *,
        child_age: int | None = None,
        persona: str | None = None,
        session_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[str]:  # type: ignore[override]
        """Yield successive text chunks for `prompt`.

        Args:
            prompt: The child's text to respond to.
            child_age: Optional age hint for tone.
            persona: Optional persona key (lively / gentle / rigorous).
            session_id: Optional session id (echoed via metadata).

        Yields:
            Successive text chunks. Empty strings are skipped by
            the SSE wrapper, so implementations may yield "" without
            harm.
        """


# ---------------------------------------------------------------------------
# Mock implementation
# ---------------------------------------------------------------------------


_FALLBACK_REPLIES = [
    "嗯……这个问题很有意思呢。",
    "因为很久以前有一颗很大的小行星撞到了地球上。",
    "扬起的尘土挡住了阳光，植物枯死了。",
    "没有植物，吃草的恐龙就没有食物。",
    "然后吃肉的恐龙也没有食物了。",
    "所以恐龙就灭绝了，但鸟类其实是恐龙的后代哦！",
]


class FallbackStreamingProvider(StreamingLLMProvider):
    """Deterministic mock that yields a fixed reply word by word.

    Defaults:
      - reply: cycles through _FALLBACK_REPLIES (deterministic by session_id)
      - delay: 50 ms between chunks
      - words_per_chunk: 2
    """

    def __init__(
        self,
        *,
        delay_seconds: float = 0.05,
        words_per_chunk: int = 2,
    ) -> None:
        self._delay = delay_seconds
        self._words_per_chunk = words_per_chunk

    async def stream(
        self,
        prompt: str,
        *,
        child_age: int | None = None,
        persona: str | None = None,
        session_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[str]:
        # Pick a reply deterministically by prompt length so the same
        # input always yields the same stream.
        idx = (len(prompt) + (child_age or 0)) % len(_FALLBACK_REPLIES)
        reply = _FALLBACK_REPLIES[idx]
        # Character-level chunking — Chinese text lacks whitespace so
        # splitting on " " would yield a single chunk. We respect
        # words_per_chunk by slicing the unicode codepoints.
        chars = list(reply)
        for i in range(0, len(chars), self._words_per_chunk):
            chunk = "".join(chars[i : i + self._words_per_chunk])
            yield chunk
            # Pause briefly so the stream is observable end-to-end.
            await asyncio.sleep(self._delay)


# ---------------------------------------------------------------------------
# Real streaming implementation — Minimax (Anthropic SSE)
# ---------------------------------------------------------------------------


class MinimaxStreamingProvider(StreamingLLMProvider):
    """Streams real tokens from Minimax via its Anthropic-compatible SSE.

    Used when `MINIMAX_API_KEY` is set (the factory wires this as the
    process-wide provider). Delegates the actual HTTP + SSE parsing to
    `MinimaxClient.stream(...)`; this class only adapts the prompt into
    a `messages` array and yields the text chunks it returns.

    Unlike the `/chat/full` agent pipeline (which injects the Niki
    persona + age guardrails into the system prompt), this raw SSE
    endpoint must do the same explicitly — otherwise the model
    introduces itself as "MiniMax-M3" instead of Niki.
    """

    def __init__(self, client: Any = None) -> None:
        # Lazy: resolve the factory singleton once, so tests can inject
        # their own client and so we share the connection pool.
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from niki.infrastructure.llm.factory import get_or_create_llm

        return get_or_create_llm()

    async def stream(
        self,
        prompt: str,
        *,
        child_age: int | None = None,
        persona: str | None = None,
        session_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[str]:
        client = self._get_client()
        # If the factory is in stub mode, fall back to the deterministic
        # mock so the SSE contract stays exercisable without a key.
        if getattr(client, "provider_name", "") == "stub":
            async for chunk in FallbackStreamingProvider().stream(
                prompt,
                child_age=child_age,
                persona=persona,
                session_id=session_id,
            ):
                yield chunk
            return

        messages = [
            *(history or []),
            {"role": "user", "content": prompt},
        ]
        # ISS-XX: MinimaxClient exposes streaming via chat_stream which
        # returns the raw httpx response (NOT an async iterator).
        # Adapter: iterate response.iter_lines() and extract text chunks.
        chat_stream = getattr(client, "chat_stream", None) or getattr(client, "stream", None)
        if chat_stream is None:
            raise RuntimeError(
                f"{type(client).__name__} has no streaming method "
                "(expected .chat_stream() or .stream())"
            )
        response = await chat_stream(
            messages,
            temperature=0.7,
            max_tokens=1024,
            system=_build_streaming_system_prompt(
                persona=persona,
                child_age=child_age,
            ),
        )
        # response is an httpx.Response with iter_lines()
        async for line in response.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                import json as _json
                obj = _json.loads(data)
                delta = obj.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield content
            except Exception:
                continue


# Shared system-prompt builder for the raw streaming endpoint.
# Kept module-level so tests can assert on it and so it mirrors the
# agent pipeline's persona + age-guardrail injection (see
# `niki.agents.base.BaseAgent`).
_PERSONA_FRAGMENTS: dict[str, str] = {
    "lively": (
        "你是一个热情阳光的AI伙伴，名叫Niki。"
        "多用感叹号和拟声词，说「哇塞」「太酷了」——像阳光般热情。"
        "用活泼的语气和小朋友交流。"
    ),
    "gentle": (
        "你是一个温柔耐心的AI伙伴，名叫Niki。"
        "说话轻柔缓慢，多用拥抱和鼓励的话语——像云朵般柔软。"
        "像耐心的幼儿园老师一样呵护小朋友。"
    ),
    "rigorous": (
        "你是一个可靠严谨的AI伙伴，名叫Niki。"
        "说话准确认真，不夸夸其谈也不冷漠——像图书管理员般可靠。"
        "用清晰有条理的方式帮助小朋友理解世界。"
    ),
}

_AGE_GUARDRAILS: dict[int, str] = {
    5: "每句话不超过10个字。每次回答不超过5句话。",
    8: "每句话不超过15个字。每次回答不超过8句话。",
    12: "每句话不超过20个字。每次回答不超过12句话。",
}


def _build_streaming_system_prompt(
    *,
    persona: str | None = None,
    child_age: int | None = None,
) -> str:
    """Build the Niki identity + persona + age guardrails for the raw
    streaming path.

    Without this the model answers as its own identity (e.g. "我是
    MiniMax-M3") instead of Niki, and ignores age-appropriate length
    limits. Mirrors the agent pipeline's `get_system_prompt_with_persona()`.
    """
    persona_frag = _PERSONA_FRAGMENTS.get(persona or "gentle", _PERSONA_FRAGMENTS["gentle"])

    guardrail = ""
    if child_age is not None:
        for age, text in sorted(_AGE_GUARDRAILS.items()):
            if child_age <= age:
                guardrail = text
                break
        if not guardrail:
            guardrail = _AGE_GUARDRAILS[12]

    return (
        f"{persona_frag}\n\n"
        "你是儿童智能陪伴助手，名字叫Niki，不是别的AI模型。\n"
        "当小朋友问「你是谁」时，回答「我是Niki，你的AI小伙伴」。\n"
        "【规则】\n"
        "1. 不知道就诚实说「Niki也不确定」，绝不编造。\n"
        "2. 用简单、生动、有趣的比喻解释。\n"
        "3. 避免恐惧词汇（死亡、怪物、灾难等），用更温柔的词替代。\n"
        "4. 绝不询问住址、电话等隐私信息。\n"
        "5. 回答要简短、友好、充满鼓励。\n" + (f"【年龄适配】{guardrail}\n" if guardrail else "")
    )


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


def _default_provider() -> StreamingLLMProvider:
    """Return MinimaxStreamingProvider when a real key is configured,
    else the deterministic mock.

    Resolving lazily (not at import time) so a key set after import
    still takes effect, and so `reset_for_tests()` can rebuild.
    """
    from niki.infrastructure.llm.factory import get_or_create_llm

    llm = get_or_create_llm()
    if getattr(llm, "provider_name", "") != "stub":
        return MinimaxStreamingProvider(client=llm)
    return FallbackStreamingProvider()


_DEFAULT_MOCK: StreamingLLMProvider = FallbackStreamingProvider()
_provider: StreamingLLMProvider = _DEFAULT_MOCK


def get_streaming_provider() -> StreamingLLMProvider:
    """Return the process-wide streaming LLM provider.

    Resolves the real provider on first access (after the LLM factory
    has had a chance to see MINIMAX_API_KEY). Tests can override via
    `set_streaming_provider(...)`; `reset_for_tests()` rebuilds the
    default.
    """
    global _provider
    if _provider is _DEFAULT_MOCK:
        # First access — decide real vs mock once, then cache.
        candidate = _default_provider()
        if not isinstance(candidate, FallbackStreamingProvider):
            _provider = candidate
    return _provider


def set_streaming_provider(provider: StreamingLLMProvider) -> None:
    """Override the process-wide streaming provider (for tests / real swap)."""
    global _provider
    _provider = provider


def reset_for_tests() -> None:
    """Restore the default streaming provider so the next call re-resolves."""
    global _provider
    _provider = _DEFAULT_MOCK
