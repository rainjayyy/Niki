"""Streaming LLM provider abstraction (BE-RT-001).

The contract:
    `stream(prompt, **kwargs)` is an async generator that yields
    successive text tokens. Callers wrap the stream in SSE or
    WebSocket responses.

Two implementations ship:
    - `MockStreamingProvider` — default; deterministic word-by-word
      emission so tests are reproducible and demos feel real.
    - `StreamingLLMProvider`  — abstract base; production plug-in
                                point for DeepSeek-V4 / doubao
                                streaming mode (BE-RT-001-real).
"""

from __future__ import annotations

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
    ) -> AsyncIterator[str]:
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


_MOCK_REPLIES = [
    "嗯……这个问题很有意思呢。",
    "因为很久以前有一颗很大的小行星撞到了地球上。",
    "扬起的尘土挡住了阳光，植物枯死了。",
    "没有植物，吃草的恐龙就没有食物。",
    "然后吃肉的恐龙也没有食物了。",
    "所以恐龙就灭绝了，但鸟类其实是恐龙的后代哦！",
]


class MockStreamingProvider(StreamingLLMProvider):
    """Deterministic mock that yields a fixed reply word by word.

    Defaults:
      - reply: cycles through _MOCK_REPLIES (deterministic by session_id)
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
    ) -> AsyncIterator[str]:
        # Pick a reply deterministically by prompt length so the same
        # input always yields the same stream.
        idx = (len(prompt) + (child_age or 0)) % len(_MOCK_REPLIES)
        reply = _MOCK_REPLIES[idx]
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
# Process-wide singleton
# ---------------------------------------------------------------------------


_provider: StreamingLLMProvider = MockStreamingProvider()


def get_streaming_provider() -> StreamingLLMProvider:
    """Return the process-wide streaming LLM provider (default: mock)."""
    return _provider


def set_streaming_provider(provider: StreamingLLMProvider) -> None:
    """Override the process-wide streaming provider (for tests / real swap)."""
    global _provider
    _provider = provider
