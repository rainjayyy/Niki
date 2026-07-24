"""Audio API endpoints (BE-MEDIA-002).

Currently exposes:
    POST /api/v1/audio/transcribe
        - multipart/form-data upload of an audio file
        - delegates to ASRProvider.transcribe(...)
        - returns {text, duration_ms, confidence, language, audio_hash}

Magic-byte validation is deliberately minimal here — the mock
provider does not actually parse audio. Real providers plugged in
via BE-MEDIA-002-real must validate format themselves before
calling their SDKs.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from niki.media.asr_provider import (
    ASRError,
    get_asr_provider,
)

router = APIRouter(prefix="/audio", tags=["audio"])

# 5 MB hard cap — covers ~30s of wav@8kHz mono, ~1m of opus.
MAX_AUDIO_BYTES = 5 * 1024 * 1024


def _audio_too_large(size: int) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={
            "error": "AUDIO_TOO_LARGE",
            "message": f"Audio exceeds {MAX_AUDIO_BYTES} bytes",
            "max_bytes": MAX_AUDIO_BYTES,
        },
    )


@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str = Form(default="zh-CN"),
    child_age: int | None = Form(default=None),
) -> dict:
    """Run ASR on an uploaded audio blob and return recognised text.

    Multipart fields:
        audio       (required) — file part
        language    (optional) — BCP-47 tag, default "zh-CN"
        child_age   (optional) — int, helps bias acoustic models

    Responses:
        200 — recognised text
        400 — empty / invalid-format audio
        413 — too large
        503 — ASR backend unavailable
    """
    # FastAPI UploadFile read is async-friendly; spools to disk by default
    # for large files but we cap at 5 MB so memory read is fine.
    raw = await audio.read()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail={"error": "EMPTY_AUDIO", "message": "audio file is empty"},
        )
    if len(raw) > MAX_AUDIO_BYTES:
        raise _audio_too_large(len(raw))

    try:
        result = await get_asr_provider().transcribe(
            raw,
            language=language,
            child_age=child_age,
        )
    except ASRError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": exc.code, "message": exc.message},
        ) from exc

    return {
        "text": result.text,
        "duration_ms": result.duration_ms,
        "confidence": result.confidence,
        "language": result.language,
        "audio_hash": hashlib.sha1(raw).hexdigest(),
    }
