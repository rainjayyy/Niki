"""Encyclopedia Agent API endpoints — child-facing knowledge Q&A.

Demonstration endpoints for the #3 vertical slice:
  POST /encyclopedia/ask   — ask a question, get a verified answer
  GET  /encyclopedia/knowledge — list the L1 knowledge entries (for debugging)

In production, the LLM call would be replaced with the real DeepSeek-V4
client. For #3 demonstration we use a stub that returns a faithful
paraphrase of the L1 hit content.
"""

from __future__ import annotations

from fastapi import APIRouter

from niki.agents.encyclopedia import EncyclopediaAgent
from niki.api._stub_llm import _EncyclopediaStub
from niki.knowledge.fact_checker import FactChecker
from niki.knowledge.knowledge_base import (
    DEFAULT_L1_ENTRIES,
    KnowledgeBase,
)
from niki.safety.input_gateway import InputSafetyGateway
from niki.schemas.common import ChildInput

router = APIRouter(
    prefix="/encyclopedia",
    tags=[
        {
            "name": "encyclopedia",
            "description": (
                "Encyclopedia Agent API endpoints — child-facing "
                "knowledge Q&A."
            ),
        },
    ],
)

# Module-level singletons
_kb = KnowledgeBase()
_fact_checker = FactChecker(_kb)
# ISS-02: a shared input gateway so self-harm / blacklisted topics
# trigger parent alert even when calling the agent directly (not via /chat).
_input_gateway = InputSafetyGateway()


_agent = EncyclopediaAgent(
    llm=_EncyclopediaStub(),
    knowledge_base=_kb,
    fact_checker=_fact_checker,
    persona="gentle",
    child_age=7,
)


# ISS-08: agent cache keyed by child_age so concurrent requests
# with different ages don't trample each other.
_agent_cache: dict[int, EncyclopediaAgent] = {}


def _get_agent(child_age: int) -> EncyclopediaAgent:
    """Return a per-child_age agent instance (cached)."""
    if child_age not in _agent_cache:
        _agent_cache[child_age] = EncyclopediaAgent(
            llm=_EncyclopediaStub(),
            knowledge_base=_kb,
            fact_checker=_fact_checker,
            persona="gentle",
            child_age=child_age,
        )
    return _agent_cache[child_age]


@router.post(
    "/ask",
    summary="Ask an encyclopedia question",
    description="""
            Ask an encyclopedia question.
            
            Request body: {"question": "...", "child_age": 7}
            
            ISS-02: run the text through InputSafetyGateway first so that
            self-harm / blacklisted topics trigger the same parent alert
            as the orchestrator path.
            """,
)
async def ask_question(payload: dict) -> dict:
    """Ask an encyclopedia question.

    Request body: {"question": "...", "child_age": 7}

    ISS-02: run the text through InputSafetyGateway first so that
    self-harm / blacklisted topics trigger the same parent alert
    as the orchestrator path.
    """
    question = payload.get("question", "")
    child_age = int(payload.get("child_age", 7))

    # ISS-02: safety gate before agent dispatch
    safety_verdict = await _input_gateway.check(question)
    if safety_verdict.is_blocked:
        return {
            "text": "嗯…这个问题Niki还没想好怎么回答呢~",
            "intent": None,
            "emotion": "neutral",
            "metadata": {"blocked": True, "reason": safety_verdict.matched_category},
        }
    if safety_verdict.is_escalation:
        return {
            "text": safety_verdict.sanitized_text or "Niki在这里~",
            "intent": "encyclopedia",
            "emotion": "caring",
            "metadata": {
                "parent_alert_required": True,
                "escalation": True,
            },
        }

    # ISS-08: use per-child_age cache to avoid concurrent requests
    # with different ages stepping on each other.
    agent = _get_agent(child_age)
    child_input = ChildInput(text=question, session_id="encyclopedia")
    output = await agent.invoke(child_input)

    return {
        "text": output.text,
        "intent": output.intent,
        "emotion": output.emotion,
        "metadata": output.metadata,
    }


@router.get(
    "/knowledge",
    summary="List L1 knowledge entries (debugging endpoint)",
    description="List L1 knowledge entries (debugging endpoint).",
)
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


@router.post(
    "/search",
    summary="Search the KB (debugging endpoint)",
    description="Search the KB (debugging endpoint).",
)
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
