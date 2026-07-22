"""Encyclopedia Agent API endpoints — child-facing knowledge Q&A.

Demonstration endpoints for the #3 vertical slice:
  POST /encyclopedia/ask   — ask a question, get a verified answer
  GET  /encyclopedia/knowledge — list the L1 knowledge entries (for debugging)

In production, the LLM call would be replaced with the real DeepSeek-V4
client. For #3 demonstration we use a stub that returns a faithful
paraphrase of the L1 hit content.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from niki.agents.encyclopedia import EncyclopediaAgent
from niki.knowledge.fact_checker import FactChecker
from niki.knowledge.knowledge_base import (
    DEFAULT_L1_ENTRIES,
    KnowledgeBase,
)
from niki.schemas.common import ChildInput

router = APIRouter(prefix="/encyclopedia", tags=["encyclopedia"])

# Module-level singletons
_kb = KnowledgeBase()
_fact_checker = FactChecker(_kb)


# A deterministic "LLM" stub for tests / dev: paraphrases the top hit.
class _StubLLM:
    """Returns a child-friendly paraphrase of the top KB hit.

    Deterministic — same input + same KB → same output. This is so
    tests can rely on stable text without real LLM calls.
    """

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.5,
        max_tokens: int = 512,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Find the user message (last one) and extract KB context
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # Pull content from the "参考资料" block
        ref_block = user_msg.split("参考资料：", 1)[1] if "参考资料：" in user_msg else user_msg

        # Return the first full L1 hit content (no truncation — keeps the
        # important keywords for the fact-checker to find).
        if ref_block.strip():
            return {"content": ref_block.strip()}
        return {"content": "嗯，Niki对这个也不太确定呢~"}


_agent = EncyclopediaAgent(
    llm=_StubLLM(),
    knowledge_base=_kb,
    fact_checker=_fact_checker,
    persona="gentle",
    child_age=7,
)


@router.post("/ask")
async def ask_question(payload: dict) -> dict:
    """Ask an encyclopedia question.

    Request body: {"question": "...", "child_age": 7}
    """
    question = payload.get("question", "")
    child_age = int(payload.get("child_age", 7))

    # Reconfigure agent for this age if needed
    if child_age != _agent.child_age:
        _agent.child_age = child_age
        _agent._age_group = _agent._resolve_age_group(child_age)
        _agent._output_gateway = _agent._output_gateway.__class__(child_age=child_age)

    child_input = ChildInput(text=question, session_id="encyclopedia")
    output = await _agent.invoke(child_input)

    return {
        "text": output.text,
        "intent": output.intent,
        "emotion": output.emotion,
        "metadata": output.metadata,
    }


@router.get("/knowledge")
async def list_knowledge() -> dict:
    """List L1 knowledge entries (debugging endpoint)."""
    return {
        "level": "L1",
        "count": len(DEFAULT_L1_ENTRIES),
        "entries": [
            {
                "id": e["id"],
                "title": e["title"],
                "tags": e.get("tags", []),
            }
            for e in DEFAULT_L1_ENTRIES
        ],
    }


@router.post("/search")
async def search_kb(payload: dict) -> dict:
    """Search the KB (debugging endpoint)."""
    query = payload.get("query", "")
    top_k = int(payload.get("top_k", 3))
    hits = _kb.search(query, top_k=top_k)
    return {
        "query": query,
        "hit_count": len(hits),
        "hits": [
            {
                "source": h.source.value,
                "score": round(h.score, 3),
                "content": h.content,
                "metadata": h.metadata,
            }
            for h in hits
        ],
    }
