"""TTS (Text-to-Speech) provider abstraction (BE-MEDIA-003).

The contract:
    - `synthesize(text, voice, speed, language)` returns a `TTSResult`
      with raw encoded audio bytes + metadata.
    - Implementations may be local (pyttsx3), cloud (Volcengine /
      iFlytek), or mock.

Two implementations ship:
    - `MockTTSProvider`  — default; emits a syntactically-valid but
                           silent WAV file. Deterministic on
                           (text, voice) so tests are reproducible.
    - `TTSProvider`      — abstract base; production implementations
                           plug in here (BE-MEDIA-003-real).

Errors are raised as `TTSError` so the FastAPI handler can map them
to §6.3-style structured 503 responses.
"""

from __future__ import annotations

import hashlib
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Result + errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TTSResult:
    """Outcome of a successful TTS synthesis."""

    audio: bytes
    mime_type: str
    duration_ms: int


class TTSError(Exception):
    """Raised when the TTS backend fails to produce audio."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------


class TTSProvider(ABC):
    """Contract for any text-to-speech implementation."""

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        voice: str = "child_friendly",
        speed: float = 1.0,
        language: str = "zh-CN",
    ) -> TTSResult:
        """Synthesise speech and return encoded audio bytes + meta.

        Args:
            text: Up to 1000 chars (validated by caller).
            voice: Voice identifier (e.g. "child_friendly", "lively",
                "gentle"). Real providers map to their own voice IDs.
            speed: 0.5 – 2.0; 1.0 = normal pace.
            language: BCP-47 tag.

        Raises:
            TTSError: Provider-specific failure.
        """


# ---------------------------------------------------------------------------
# Mock implementation — emits a silent WAV
# ---------------------------------------------------------------------------


# WAV synthesis parameters (must match each other).
_MOCK_WAV_SAMPLE_RATE = 8000      # 8 kHz, mono, 8-bit unsigned
_MOCK_WAV_BITS = 8
_MOCK_WAV_CHANNELS = 1
# Characters per second approximation; matches a slow news-reader pace.
_MOCK_CHARS_PER_SECOND = 6.5


def _make_silent_wav(text: str, speed: float) -> tuple[bytes, int]:
    """Build a syntactically-valid but silent WAV file.

    Returns ``(audio_bytes, duration_ms)``. The audio is deterministic
    so the same (text, speed) always yields byte-identical output.
    """
    # Each char ≈ 1/_MOCK_CHARS_PER_SECOND seconds; speed scales it.
    duration_seconds = max(0.5, len(text) / _MOCK_CHARS_PER_SECOND / speed)
    duration_ms = int(duration_seconds * 1000)
    num_samples = int(duration_seconds * _MOCK_WAV_SAMPLE_RATE)

    # PCM payload: silence is 0x80 for unsigned 8-bit.
    pcm = bytes([0x80]) * num_samples

    byte_rate = _MOCK_WAV_SAMPLE_RATE * _MOCK_WAV_CHANNELS * (_MOCK_WAV_BITS // 8)
    block_align = _MOCK_WAV_CHANNELS * (_MOCK_WAV_BITS // 8)
    data_size = len(pcm)

    # RIFF header
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,    # 36 = header size minus 8 for "RIFF<size>"
        b"WAVE",
        b"fmt ",
        16,                # fmt chunk size for PCM
        1,                 # audio format = PCM
        _MOCK_WAV_CHANNELS,
        _MOCK_WAV_SAMPLE_RATE,
        byte_rate,
        block_align,
        _MOCK_WAV_BITS,
        b"data",
        data_size,
    )
    return header + pcm, duration_ms


class MockTTSProvider(TTSProvider):
    """Deterministic TTS producing silent but structurally-valid WAV."""

    async def synthesize(
        self,
        text: str,
        *,
        voice: str = "child_friendly",
        speed: float = 1.0,
        language: str = "zh-CN",
    ) -> TTSResult:
        if not text or not text.strip():
            raise TTSError("EMPTY_TEXT", "Input text is empty")

        wav, duration_ms = _make_silent_wav(text, speed)
        return TTSResult(
            audio=wav,
            mime_type="audio/wav",
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_provider: TTSProvider = MockTTSProvider()


def get_tts_provider() -> TTSProvider:
    """Return the process-wide TTS provider (default: mock)."""
    return _provider


def set_tts_provider(provider: TTSProvider) -> None:
    """Override the process-wide TTS provider (for tests / real swap)."""
    global _provider
    _provider = provider
