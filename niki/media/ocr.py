"""OCR (Optical Character Recognition) provider abstraction (BE-MEDIA-005).

The contract:
    - `recognize(image_bytes, language)` returns an `OcrResult`
      with extracted text, bounding regions, and metadata.
    - Implementations may be local (pytesseract), cloud
      (Aliyun / Tencent OCR), or mock.

Two implementations ship:
    - `MockOcrBackend`  — default; deterministic, hash-bucketed text
                          from a small dictionary. No external deps.
    - `OcrBackend`      — abstract base; production implementations
                          plug in here (BE-MEDIA-005-real).

Errors are raised as `OcrError` so the FastAPI handler can map them
to structured 503 responses.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable


# ---------------------------------------------------------------------------
# Result + errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OcrRegion:
    """A single detected text region inside the image."""

    text: str
    confidence: float
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass(frozen=True)
class OcrResult:
    """Outcome of a successful OCR call."""

    text: str
    language: str
    confidence: float
    regions: tuple[OcrRegion, ...] = ()


class OcrError(Exception):
    """Raised when the OCR backend fails to produce a result."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------


class OcrBackend(ABC):
    """Contract for any OCR implementation."""

    @abstractmethod
    async def recognize(
        self,
        image: bytes,
        *,
        language: str = "zh-CN",
    ) -> OcrResult:
        """Recognise `image` and return extracted text + meta.

        Args:
            image: Raw encoded image bytes (png/jpg/webp/gif).
            language: BCP-47 language tag. Default "zh-CN".

        Raises:
            OcrError: Provider-specific failure (timeout, malformed,
                quota, etc). Caller maps to HTTP 503.
        """


# ---------------------------------------------------------------------------
# Mock implementation
# ---------------------------------------------------------------------------


# A canned dictionary of phrases keyed by hash bucket. The mock
# picks 3-5 words from a couple of buckets and joins them as if
# they were words spotted on the page. Same bytes -> same text,
# so tests / dev demos are reproducible.
_MOCK_WORDS_ZH = [
    "苹果",
    "香蕉",
    "学校",
    "太阳",
    "月亮",
    "星星",
    "海豚",
    "恐龙",
    "妈妈",
    "爸爸",
    "猫",
    "狗",
    "树",
    "花",
    "书",
    "桌子",
    "椅子",
    "汽车",
    "飞机",
    "火车",
]

_MOCK_WORDS_EN = [
    "apple",
    "banana",
    "school",
    "sun",
    "moon",
    "star",
    "dolphin",
    "dinosaur",
    "mama",
    "papa",
    "cat",
    "dog",
    "tree",
    "flower",
    "book",
    "table",
    "chair",
    "car",
    "plane",
    "train",
]

# How many words the mock "spots" per image (between 3 and 5).
_MOCK_MIN_WORDS = 3
_MOCK_MAX_WORDS = 5


def _pick_words(digest: bytes, n: int, words: Iterable[str]) -> list[str]:
    """Deterministically pick `n` words from `words`.

    Uses consecutive bytes of `digest` as bucket indices so the
    same image always yields the same selection. `_MOCK_MAX_WORDS`
    is bounded so we never index out of range.
    """
    bucket = list(words)
    if n < 1 or not bucket:
        return []
    out: list[str] = []
    used: set[int] = set()
    for byte in digest:
        if len(out) >= n:
            break
        idx = byte % len(bucket)
        if idx in used:
            continue
        used.add(idx)
        out.append(bucket[idx])
    return out


class MockOcrBackend(OcrBackend):
    """Deterministic, no-deps OCR for PoC and tests.

    Hashes the image bytes, then picks a small handful of "spotted"
    words from a fixed dictionary keyed by language. Confidence is
    a stable 0.91 — high enough to demonstrate the field but not so
    close to 1.0 that downstream code can't distinguish mock from
    real.

    Production swap-ins implement `OcrBackend` directly and plug
    in via `set_ocr_backend(...)`.
    """

    async def recognize(
        self,
        image: bytes,
        *,
        language: str = "zh-CN",
    ) -> OcrResult:
        if not image:
            raise OcrError("EMPTY_IMAGE", "Image payload is empty")

        digest = hashlib.sha1(image).digest()

        # Word count: bytes 4-5 mapped to [MIN, MAX].
        n = _MOCK_MIN_WORDS + (
            (digest[4] << 8 | digest[5]) % (_MOCK_MAX_WORDS - _MOCK_MIN_WORDS + 1)
        )

        # English dictionary for en-* / default unknown zh-* only when
        # caller explicitly asked for English — otherwise stick with zh.
        words = _MOCK_WORDS_EN if language.lower().startswith("en") else _MOCK_WORDS_ZH
        picked = _pick_words(digest, n, words)
        text = " ".join(picked) if picked else ""

        # Per-region confidence: stable 0.91 with a tiny bit of
        # pseudo-variance from the digest so adjacent regions look
        # distinct in tests.
        regions = tuple(
            OcrRegion(
                text=w,
                confidence=round(0.88 + (digest[i] % 8) / 100.0, 3),
                bbox=(i * 10, 0, (i + 1) * 10, 16),
            )
            for i, w in enumerate(picked)
        )
        avg_conf = round(sum(r.confidence for r in regions) / len(regions), 3) if regions else 0.0

        return OcrResult(
            text=text,
            language=language,
            confidence=avg_conf,
            regions=regions,
        )


# ---------------------------------------------------------------------------
# Process-wide singleton (production swap-in point)
# ---------------------------------------------------------------------------


_backend: OcrBackend = MockOcrBackend()


def get_ocr_backend() -> OcrBackend:
    """Return the process-wide OCR backend (default: mock)."""
    return _backend


def set_ocr_backend(backend: OcrBackend) -> None:
    """Override the process-wide OCR backend (for tests / real swap)."""
    global _backend
    _backend = backend
