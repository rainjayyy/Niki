"""SSE chat streaming endpoint (BE-RT-001).

Implements:
    POST /api/v1/chat/stream
        - JSON body {text, session_id, child_age, persona}
        - returns text/event-stream with successive {chunk: ...}
          events followed by a final metadata event and a [DONE]
          sentinel.

The provider pulls chunks from `StreamingLLMProvider.stream(...)`
and pushes them into an async generator that yields text/event-stream
formatted lines, ready for consumption by `EventSource` (browser)
or `httpx.stream` (Python).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Body, HTTPException, status
from fastapi.responses import StreamingResponse

from niki.infrastructure.llm.streaming import get_streaming_provider

router = APIRouter(prefix="/chat", tags=["chat"])


def _format_sse(data: dict | str) -> bytes:
    """Format a single SSE event line.

    Each event is `data: <json>\\n\\n` per the SSE spec.
    """
    if isinstance(data, str):
        payload = data
    else:
        payload = json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


async def _sse_generator(
    prompt: str,
    *,
    child_age: int | None,
    persona: str | None,
    session_id: str | None,
) -> AsyncIterator[bytes]:
    """Yield SSE-formatted chunks from the streaming provider."""
    full_text_parts: list[str] = []
    token_index = 0

    async for chunk in get_streaming_provider().stream(
        prompt,
        child_age=child_age,
        persona=persona,
        session_id=session_id,
    ):
        if chunk:  # skip blanks from the provider
            full_text_parts.append(chunk)
            yield _format_sse(
                {"chunk": chunk, "token_index": token_index}
            )
            token_index += 1

    # Final metadata event before [DONE] sentinel.
    yield _format_sse(
        {
            "session_id": session_id,
            "total_text": "".join(full_text_parts),
            "is_final": True,
        }
    )
    yield _format_sse("[DONE]")


@router.post("/stream")
async def chat_stream(
    body: dict = Body(...),
) -> StreamingResponse:
    """Stream a chat reply via Server-Sent Events.

    Body fields:
        text       (required) — child's input
        session_id (optional)
        child_age  (optional) — int
        persona    (optional) — string

    Response: text/event-stream (see docs/be-issues/BE-RT-001.md).
    """
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "EMPTY_TEXT", "message": "text is required"},
        )
    if len(text) > 1000:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "TEXT_TOO_LONG",
                "message": "text exceeds 1000 chars",
            },
        )

    session_id = body.get("session_id")
    child_age = body.get("child_age")
    persona = body.get("persona")

    gen = _sse_generator(
        text,
        child_age=child_age,
        persona=persona,
        session_id=session_id,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def register_status() -> int:
    """Diagnostic helper — returns 1 to confirm import works."""
    return status.HTTP_200_OK  # type: ignore[no-any-return]
