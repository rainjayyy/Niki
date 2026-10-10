"""OCR (Optical Character Recognition) provider abstraction (BE-MEDIA-005).

The contract:
    - `recognize(image_bytes, language)` returns an `OcrResult`
      with extracted text, bounding regions, and metadata.

Implementations:
    - `PytesseractOcrBackend` — local Tesseract via pytesseract.
                                  Best for dev/test; zero API cost.
                                  Requires the `tesseract` binary + chi_sim
                                  language pack on the host.
    - `HttpOcrBackend`       — forwards to a cloud OCR endpoint
                                  (Aliyun / Tencent). Enable via
                                  OCR_BASE_URL.
    - `FallbackOcrBackend`   — deterministic hash-bucketed mock.
                                  Last-resort fallback when no real
                                  backend is available; useful only
                                  for PoC and unit tests.

Errors are raised as `OcrError` so the FastAPI handler can map them
to structured 503 responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, TYPE_CHECKING


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
        """


# ---------------------------------------------------------------------------
# Real implementations
# ---------------------------------------------------------------------------


# Tesseract uses traineddata filenames (3-letter ISO 639-2 / -t variants).
# Map common BCP-47 tags to what tesseract understands; default to
# Simplified Chinese + English combined for the typical Niki case.
_LANG_BCP47_TO_TESS: dict[str, str] = {
    "zh-CN": "chi_sim+eng",
    "zh-HK": "chi_tra+eng",
    "zh-TW": "chi_tra+eng",
    "zh": "chi_sim+eng",
    "en": "eng",
    "en-US": "eng",
    "en-GB": "eng",
    "ja": "jpn",
    "ko": "kor",
    "fr": "fra",
    "de": "deu",
    "es": "spa",
    "ru": "rus",
}


def _tesseract_available() -> bool:
    """Return True iff the `tesseract` binary is on PATH (or pointed to via env)."""
    explicit = os.environ.get("OCR_TESSERACT_CMD", "").strip()
    if explicit and os.path.isfile(explicit):
        return True
    return shutil.which("tesseract") is not None


class PytesseractOcrBackend(OcrBackend):
    """Local OCR via the `tesseract` binary, driven by `pytesseract`.

    Requirements on the deployment machine:
        - `tesseract` binary on PATH (apt: `tesseract-ocr`,
          brew: `tesseract`, Windows: tesseract-ocr installer)
        - Language packs for the languages you want (apt: tesseract-osd,
          tesseract-eng, tesseract-chi-sim for Chinese)

    Synchronous tesseract calls run in a worker thread via
    `asyncio.to_thread` so the FastAPI event loop stays unblocked.
    """

    def __init__(
        self,
        *,
        tesseract_cmd: str | None = None,
        default_lang: str = "en",
    ) -> None:
        self._tesseract_cmd = tesseract_cmd
        self._default_lang = default_lang

    async def recognize(
        self,
        image: bytes,
        *,
        language: str = "zh-CN",
    ) -> OcrResult:
        if not image:
            raise OcrError("EMPTY_IMAGE", "Image payload is empty")
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            raise OcrError(
                "OCR_DEPS_MISSING",
                "pytesseract/Pillow not installed. Run: pip install pytesseract Pillow",
            ) from exc

        tess_lang = _LANG_BCP47_TO_TESS.get(language, self._default_lang)
        kw: dict[str, object] = {"lang": tess_lang, "output_type": pytesseract.Output.DICT}
        if self._tesseract_cmd:
            kw["config"] = self._tesseract_cmd

        def _recognise() -> OcrResult:
            img = Image.open(io.BytesIO(image))
            data = pytesseract.image_to_data(img, **kw)
            lines: dict[int, list[str]] = {}
            confs: dict[int, list[float]] = {}
            for word, conf, line_num in zip(data["text"], data["conf"], data["line_num"]):
                if not word.strip():
                    continue
                lines.setdefault(line_num, []).append(word)
                confs.setdefault(line_num, []).append(float(conf) / 100.0)
            regions_list: list[OcrRegion] = []
            for line_num, words in lines.items():
                avg = (
                    sum(confs[line_num]) / len(confs[line_num])
                    if confs[line_num]
                    else 0.0
                )
                regions_list.append(
                    OcrRegion(
                        text=" ".join(words),
                        confidence=round(avg, 3),
                        bbox=(0, line_num * 16, len(words) * 10, (line_num + 1) * 16),
                    )
                )
            full_text = " ".join(" ".join(ws) for _, ws in sorted(lines.items()))
            avg_conf = (
                round(sum(r.confidence for r in regions_list) / len(regions_list), 3)
                if regions_list
                else 0.0
            )
            return OcrResult(
                text=full_text,
                language=language,
                confidence=avg_conf,
                regions=tuple(regions_list),
            )

        try:
            return await asyncio.to_thread(_recognise)
        except pytesseract.TesseractNotFoundError as exc:
            raise OcrError(
                "OCR_BINARY_MISSING",
                "tesseract binary not found on PATH. Install tesseract-ocr + language packs.",
            ) from exc
        except pytesseract.TesseractError as exc:
            raise OcrError("OCR_TESSERACT_ERROR", str(exc)) from exc


class HttpOcrBackend(OcrBackend):
    """Cloud OCR over HTTP — forwards the image to a remote endpoint.

    Enable by setting:
        OCR_BASE_URL  — required, e.g. an Aliyun OCR gateway or a
                         self-hosted Tesseract HTTP wrapper
        OCR_API_KEY    — Bearer credential in the Authorization header
        OCR_MODEL      — backend-specific model id (form field)
        OCR_TIMEOUT    — request timeout in seconds (default 30)
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout_s: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s

    async def recognize(
        self,
        image: bytes,
        *,
        language: str = "zh-CN",
    ) -> OcrResult:
        if not image:
            raise OcrError("EMPTY_IMAGE", "Image payload is empty")
        try:
            import httpx
        except ImportError as exc:
            raise OcrError("OCR_DEPS_MISSING", "httpx not installed") from exc

        files = {"file": ("image", image)}
        data: dict[str, object] = {"language": language}
        if self._model:
            data["model"] = self._model
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/recognize",
                    files=files,
                    data=data,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise OcrError("OCR_HTTP_FAILED", f"HTTP error: {exc}") from exc

        if resp.status_code >= 400:
            raise OcrError(
                "OCR_BACKEND_ERROR",
                f"Backend returned {resp.status_code}: {resp.text[:200]}",
            )

        body = resp.json()
        try:
            text = str(body.get("text", ""))
            raw_regions = body.get("regions", []) or []
            regions = tuple(
                OcrRegion(
                    text=str(r.get("text", "")),
                    confidence=float(r.get("confidence", 0.0)),
                    bbox=tuple(r.get("bbox", (0, 0, 0, 0))),
                )
                for r in raw_regions
            )
            avg = (
                round(sum(r.confidence for r in regions) / len(regions), 3)
                if regions
                else 0.0
            )
            return OcrResult(
                text=text,
                language=language,
                confidence=avg,
                regions=regions,
            )
        except (TypeError, ValueError) as exc:
            raise OcrError(
                "OCR_BACKEND_BAD_RESPONSE", f"Malformed backend response: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Fallback (only when no real backend is configured)
# ---------------------------------------------------------------------------


_MOCK_WORDS_ZH = [
    "月亮", "星星", "海豚", "恐龙", "妈妈",
    "爸爸", "猫", "狗", "树", "花", "书", "桌子",
    "椅子", "汽车", "飞机", "火车",
]
_MOCK_WORDS_EN = [
    "apple", "banana", "school", "sun", "moon", "star",
    "dolphin", "dinosaur", "mama", "papa", "cat", "dog",
    "tree", "flower", "book", "table", "chair", "car", "plane", "train",
]
_MOCK_MIN_WORDS = 3
_MOCK_MAX_WORDS = 5


def _pick_words(digest: bytes, n: int, words: Iterable[str]) -> list[str]:
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


class FallbackOcrBackend(OcrBackend):
    """Deterministic, no-deps OCR used only when no real backend is
    available (no tesseract, no OCR_BASE_URL).

    Hashes the image bytes, then picks a small handful of "spotted"
    words from a fixed dictionary keyed by language. Confidence is a
    stable 0.91 with a tiny bit of pseudo-variance.

    NOT for production use. Replace with:
        set_ocr_backend(PytesseractOcrBackend(...))
        set_ocr_backend(HttpOcrBackend(...))
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
        n = _MOCK_MIN_WORDS + (
            (digest[4] << 8 | digest[5]) % (_MOCK_MAX_WORDS - _MOCK_MIN_WORDS + 1)
        )
        words = _MOCK_WORDS_EN if language.lower().startswith("en") else _MOCK_WORDS_ZH
        picked = _pick_words(digest, n, words)

        regions = tuple(
            OcrRegion(
                text=w,
                confidence=round(0.88 + (digest[i] % 8) / 100.0, 3),
                bbox=(i * 10, 0, (i + 1) * 10, 16),
            )
            for i, w in enumerate(picked)
        )
        avg = round(sum(r.confidence for r in regions) / len(regions), 3) if regions else 0.0
        return OcrResult(
            text=" ".join(picked) if picked else "",
            language=language,
            confidence=avg,
            regions=regions,
        )


# ---------------------------------------------------------------------------
# Process-wide singleton (auto-selects the best available backend)
# ---------------------------------------------------------------------------


def _build_default_backend() -> OcrBackend:
    """Pick the best available backend in priority order:

    1. `OCR_BASE_URL` set          -> HttpOcrBackend
    2. `tesseract` binary on PATH  -> PytesseractOcrBackend
    3. fallback                      -> FallbackOcrBackend
    """
    base_url = os.environ.get("OCR_BASE_URL", "").strip()
    if base_url:
        return HttpOcrBackend(
            base_url=base_url,
            api_key=os.environ.get("OCR_API_KEY", ""),
            model=os.environ.get("OCR_MODEL", ""),
            timeout_s=float(os.environ.get("OCR_TIMEOUT", "30")),
        )
    if _tesseract_available():
        try:
            import pytesseract  # noqa: F401
            return PytesseractOcrBackend(
                tesseract_cmd=os.environ.get("OCR_TESSERACT_CMD") or None,
                default_lang=os.environ.get("OCR_LANG", "en"),
            )
        except ImportError:
            pass
    return FallbackOcrBackend()


_backend: OcrBackend = _build_default_backend()


def get_ocr_backend() -> OcrBackend:
    """Return the process-wide OCR backend."""
    return _backend


def set_ocr_backend(backend: OcrBackend) -> None:
    """Override the process-wide OCR backend (for tests / real swap)."""
    global _backend
    _backend = backend
