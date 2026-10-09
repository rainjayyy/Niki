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
# Configurable HTTP provider
# ---------------------------------------------------------------------------


class HttpASRProvider(ASRProvider):
    """ASR provider that forwards audio to an HTTP endpoint.

    Enable by setting ASR_BASE_URL (optionally ASR_API_KEY / ASR_MODEL).
    The endpoint contract mirrors POST /api/v1/audio/transcribe:
    multipart field `file`, optional form fields `language` and
    `child_age`; response JSON `{text, duration_ms, confidence}`.
    """

    def __init__(self, base_url: str, api_key: str = "", model: str = "whisper-1") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "zh-CN",
        child_age: int | None = None,
    ) -> ASRResult:
        import httpx

        files = {"file": ("audio.bin", audio)}
        data: dict[str, object] = {"language": language, "model": self._model}
        if child_age is not None:
            data["child_age"] = child_age
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._base_url}/transcribe",
                    files=files,
                    data=data,
                    headers=headers,
                )
        except Exception as exc:
            raise ASRError("ASR_BACKEND_UNAVAILABLE", str(exc)) from exc
        if resp.status_code != 200:
            raise ASRError(
                "ASR_HTTP_ERROR",
                f"ASR backend returned HTTP {resp.status_code}",
            )
        body = resp.json()
        return ASRResult(
            text=str(body.get("text", "")),
            duration_ms=int(body.get("duration_ms", 0)),
            confidence=float(body.get("confidence", 0.9)),
            language=language,
        )


# ---------------------------------------------------------------------------
# Process-wide singleton (production swap-in point)
# ---------------------------------------------------------------------------


_provider: ASRProvider | None = None


def get_asr_provider() -> ASRProvider:
    """Return the process-wide ASR provider.

    Uses HttpASRProvider when ASR_BASE_URL is configured, otherwise
    falls back to the deterministic mock.
    """
    import os

    global _provider
    if _provider is None:
        base_url = os.environ.get("ASR_BASE_URL", "").strip()
        if base_url:
            _provider = HttpASRProvider(
                base_url,
                api_key=os.environ.get("ASR_API_KEY", ""),
                model=os.environ.get("ASR_MODEL", "whisper-1"),
            )
        else:
            _provider = MockASRProvider()
    return _provider


def set_asr_provider(provider: ASRProvider) -> None:
    """Override the process-wide ASR provider (for tests / real swap)."""
    global _provider
    _provider = provider
