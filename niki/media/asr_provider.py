"""ASR (Automatic Speech Recognition) provider abstraction (BE-MEDIA-002).

The contract:
    - `transcribe(audio, language, child_age)` returns an `ASRResult`
      with the recognised text and metadata.
    - Implementations may be local stubs (mock), on-prem (whisper.cpp),
      or cloud (Alibaba / Tencent one-shot recognition).

Two implementations ship:
    - `MockASRProvider`  — default; deterministic, hash-based text for
                           testing and dev demos without external deps.
    - `ASRProvider`      — abstract base; production implementations
                           plug in here (BE-MEDIA-002-real).

Errors are raised as `ASRError` so the FastAPI handler can map them
to §6.3-style structured 503 responses.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Result + errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ASRResult:
    """Outcome of a successful ASR call."""

    text: str
    duration_ms: int
    confidence: float
    language: str


class ASRError(Exception):
    """Raised when the ASR backend fails to produce a result."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------


class ASRProvider(ABC):
    """Contract for any speech-to-text implementation."""

    @abstractmethod
    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "zh-CN",
        child_age: int | None = None,
    ) -> ASRResult:
        """Recognise `audio` (any common codec) and return text + meta.

        Args:
            audio: Raw encoded bytes (wav/m4a/opus/mp3).
            language: BCP-47 language tag. Default "zh-CN".
            child_age: Used by real providers to bias acoustic models
                towards child speech; mocked in this implementation.

        Raises:
            ASRError: Provider-specific failure (timeout, malformed,
                quota, etc). Caller maps to HTTP 503.
        """


# ---------------------------------------------------------------------------
# Mock implementation
# ---------------------------------------------------------------------------


# A canned corpus of phrases keyed by hash bucket. Tests/demo pick from
# here so the same audio always yields the same text, and the demo
# receives something that "looks real".
_MOCK_PHRASES_ZH = [
    "Niki，你好",
    "今天我学了一个新单词",
    "我想知道为什么天空是蓝色的",
    "恐龙为什么会灭绝？",
    "妈妈什么时候回来？",
    "作业太难了",
    "我不想睡觉",
    "我们一起玩游戏吧",
    "为什么要有考试？",
    "可以给我讲个故事吗",
]


class MockASRProvider(ASRProvider):
    """Deterministic, no-deps ASR for PoC and tests."""

    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "zh-CN",
        child_age: int | None = None,
    ) -> ASRResult:
        if not audio:
            raise ASRError("EMPTY_AUDIO", "Audio payload is empty")

        digest = hashlib.sha1(audio).digest()
        bucket = digest[0] % len(_MOCK_PHRASES_ZH)
        text = _MOCK_PHRASES_ZH[bucket]
        # Pseudo duration derived from bytes (last 2 bytes mod a few seconds).
        duration_ms = int.from_bytes(digest[-2:], "big") % 5000 + 500
        return ASRResult(
            text=text,
            duration_ms=duration_ms,
            confidence=0.92,
            language=language,
        )


# ---------------------------------------------------------------------------
# Process-wide singleton (production swap-in point)
# ---------------------------------------------------------------------------


_provider: ASRProvider = MockASRProvider()


def get_asr_provider() -> ASRProvider:
    """Return the process-wide ASR provider (default: mock)."""
    return _provider


def set_asr_provider(provider: ASRProvider) -> None:
    """Override the process-wide ASR provider (for tests / real swap)."""
    global _provider
    _provider = provider
