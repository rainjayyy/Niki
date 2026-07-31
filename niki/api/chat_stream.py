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

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from niki.infrastructure.llm.streaming import get_streaming_provider
from niki.metrics import get_registry
from niki.middleware.rate_limit import limiter
from typing import Annotated
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent

router = APIRouter(prefix="/chat", tags=[
    {"name": "chat", "description": "SSE chat streaming endpoint (BE-RT-001)."}
])


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


@router.post(
    "/stream",
    summary="Stream a chat reply via Server-Sent Events",
    description="""
            Stream a chat reply via Server-Sent Events.
            
            Body fields:
                text       (required) — child's input
                session_id (optional)
                child_age  (optional) — int
                persona    (optional) — string
            
            Response: text/event-stream (see docs/be-issues/BE-RT-001.md).
            """,
)
@limiter.limit("30/minute")
async def chat_stream(
    request: Request,
    body: dict = Body(...),
    *,
    parent: ParentAccount = Depends(get_current_parent),
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

    # BE-API-009: count every chat-stream request.
    try:
        provider_name = type(
            get_streaming_provider()
        ).__name__.lower().replace("streamingprovider", "") or "mock"
    except Exception:  # pragma: no cover
        provider_name = "mock"
    get_registry().CHAT_TOTAL.labels(
        route="chat_stream",
        provider=provider_name,
        mode="stream",
    ).inc()

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
