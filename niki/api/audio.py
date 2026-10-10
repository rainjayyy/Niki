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

import base64
import hashlib

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile

from niki.media.asr_provider import (
    ASRError,
    get_asr_provider,
)
from niki.media.tts_provider import (
    TTSError,
    get_tts_provider,
)
from niki.middleware.rate_limit import limiter
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent

router = APIRouter(
    prefix="/audio", tags=[{"name": "audio", "description": "Audio API endpoints (BE-MEDIA-002)."}]  # type: ignore[list-item]
)

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


@router.post(
    "/transcribe",
    summary="Run ASR on an uploaded audio blob and return recognised text",
    description="""
            Run ASR on an uploaded audio blob and return recognised text.
            
            Multipart fields:
                audio       (required) — file part
                language    (optional) — BCP-47 tag, default "zh-CN"
                child_age   (optional) — int, helps bias acoustic models
            
            Responses:
                200 — recognised text
                400 — empty / invalid-format audio
                413 — too large
                503 — ASR backend unavailable
            """,
)
@limiter.limit("30/minute")
async def transcribe(
    request: Request,
    audio: UploadFile = File(...),
    language: str = Form(default="zh-CN"),
    child_age: int | None = Form(default=None),
    *,
    parent: ParentAccount = Depends(get_current_parent),
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


# ---------------------------------------------------------------------------
# BE-MEDIA-003: TTS synthesis
# ---------------------------------------------------------------------------


MAX_TEXT_CHARS = 1000


@router.post(
    "/synthesize",
    summary="Synthesise speech from text and return base64-encoded audio",
    description="""
            Synthesise speech from text and return base64-encoded audio.
            
            JSON body fields:
                text     (required) — up to 1000 chars
                voice    (optional) — default "child_friendly"
                speed    (optional) — float, default 1.0
                language (optional) — default "zh-CN"
            
            Response:
                { audio_base64, mime_type, duration_ms, voice, text_hash, size_bytes }
            """,
)
@limiter.limit("60/minute")
async def synthesize(
    request: Request,
    body: dict = Body(...),
    *,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Synthesise speech from text and return base64-encoded audio.

    JSON body fields:
        text     (required) — up to 1000 chars
        voice    (optional) — default "child_friendly"
        speed    (optional) — float, default 1.0
        language (optional) — default "zh-CN"

    Response:
        { audio_base64, mime_type, duration_ms, voice, text_hash, size_bytes }
    """
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "EMPTY_TEXT", "message": "text is required"},
        )
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "TEXT_TOO_LONG",
                "message": f"text exceeds {MAX_TEXT_CHARS} chars",
                "max_chars": MAX_TEXT_CHARS,
            },
        )

    voice = body.get("voice", "child_friendly")
    speed = float(body.get("speed", 1.0))
    language = body.get("language", "zh-CN")

    try:
        result = await get_tts_provider().synthesize(
            text,
            voice=voice,
            speed=speed,
            language=language,
        )
    except TTSError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": exc.code, "message": exc.message},
        ) from exc

    return {
        "audio_base64": base64.b64encode(result.audio).decode("ascii"),
        "mime_type": result.mime_type,
        "duration_ms": result.duration_ms,
        "voice": voice,
        "text_hash": hashlib.sha1(text.encode("utf-8")).hexdigest(),
        "size_bytes": len(result.audio),
    }
