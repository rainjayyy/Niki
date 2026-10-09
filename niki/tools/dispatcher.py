"""Tool-call dispatcher — BE-AGENT-001.

The dispatcher executes a single OpenAI ``tool_call`` dict and
returns the string that should be placed in a
``role: "tool"`` message for the next LLM turn.

Design rules:

* **Never raises** on bad input. Malformed JSON arguments, unknown
  tool names, or a missing implementation all return a small JSON
  error envelope ``{"error": "..."}`` so the LLM can self-correct
  on the next round instead of crashing the agent.
* **Sync KB calls** are awaited via ``asyncio.to_thread`` because
  :meth:`niki.knowledge.knowledge_base.KnowledgeBase.search` is
  synchronous. We don't want to block the event loop on a Jaccard
  scan that walks the full L1+L2 lists.
* **Tool implementations are intentionally small** — ``time_query``
  and ``image_recognition`` are stubs. They're useful to validate
  the loop wiring end-to-end without dragging in real search APIs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import structlog

from niki.knowledge.knowledge_base import KnowledgeBase

logger = structlog.get_logger(__name__)


# Default top_k for KB search when the LLM doesn't specify one.
# Mirrors the heuristic in `EncyclopediaAgent._retrieve` (3 for 6-8,
# 2 for 3-5) — we use 3 here as a safe middle ground; the agent
# can override by passing top_k explicitly in the tool call.
_DEFAULT_KB_TOP_K = 3


async def dispatch_tool_call(
    call: dict[str, Any],
    *,
    kb: KnowledgeBase,
    image_caption: str | None = None,
) -> str:
    """Execute a single tool call and return the string result.

    Args:
        call: OpenAI-standard tool_call dict with shape
            ``{"id": "...", "type": "function", "function": {"name": "...",
            "arguments": "{...json...}"}}``. The ``arguments`` field is
            a *string*, not a dict — we JSON-decode it here.
        kb: The shared :class:`KnowledgeBase` instance.
        image_caption: Pre-computed caption for the child's
            attached image, if any. Used by
            ``image_recognition.caption``.

    Returns:
        A string suitable for the ``content`` of a
        ``role: "tool"`` message. On any error path this is a small
        JSON error envelope (still a string), not a Python exception.
    """
    fn = call.get("function") or {}
    name = fn.get("name", "")
    raw_args = fn.get("arguments", "")

    # ---- Parse arguments defensively ----
    try:
        if isinstance(raw_args, str):
            args = json.loads(raw_args) if raw_args.strip() else {}
        elif isinstance(raw_args, dict):
            # Some providers serialise the args dict directly.
            args = raw_args
        else:
            args = {}
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "tools.dispatcher.malformed_args",
            tool=name,
            error=str(exc),
        )
        return json.dumps(
            {"error": "malformed arguments", "tool": name},
            ensure_ascii=False,
        )

    # ---- Dispatch ----
    try:
        if name == "knowledge_base.search":
            query = args.get("query", "").strip()
            if not query:
                return json.dumps(
                    {"error": "empty query", "tool": name},
                    ensure_ascii=False,
                )
            top_k = int(args.get("top_k", _DEFAULT_KB_TOP_K))
            top_k = max(1, min(top_k, 5))  # clamp to schema bounds
            # KB.search is sync — push to a worker thread.
            hits = await asyncio.to_thread(kb.search, query, top_k)
            payload = [
                {
                    "id": h.metadata.get("id"),
                    "source": h.source.value,
                    "score": round(h.score, 3),
                    "content": h.content,
                }
                for h in hits
            ]
            logger.info(
                "tools.dispatcher.kb_search",
                query=query[:80],
                hit_count=len(payload),
            )
            return json.dumps(payload, ensure_ascii=False)

        if name == "time_query.now":
            now = datetime.now(timezone.utc).isoformat()
            logger.info("tools.dispatcher.time_query", result=now)
            return json.dumps({"now_utc": now}, ensure_ascii=False)

        if name == "image_recognition.caption":
            caption = image_caption or "Niki 看到的图片（暂无描述）"
            logger.info("tools.dispatcher.image_caption", has_caption=bool(image_caption))
            return json.dumps({"caption": caption}, ensure_ascii=False)

        # Unknown tool — return error envelope so the LLM can
        # self-correct on the next round.
        logger.warning("tools.dispatcher.unknown_tool", tool=name)
        return json.dumps(
            {"error": "unknown tool", "tool": name},
            ensure_ascii=False,
        )
    except Exception as exc:  # pragma: no cover — defensive
        # Last-resort: never let the dispatcher crash the agent loop.
        logger.error(
            "tools.dispatcher.unhandled",
            tool=name,
            error=str(exc),
        )
        return json.dumps(
            {"error": "internal dispatcher failure", "tool": name, "detail": str(exc)},
            ensure_ascii=False,
        )


__all__ = ["dispatch_tool_call"]
