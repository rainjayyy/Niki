"""Chat orchestrator API endpoints — the end-to-end pipeline.

Demonstration endpoint for the #6 vertical slice:
  POST /chat/full — run the full safety + classify + route + dispatch + fuse + safety pipeline

This is the integration test of all previous slices (#1 safety,
#2 routing+persona, #3 encyclopedia, #4 tutoring, #5 emotion).
"""

from __future__ import annotations

from fastapi import APIRouter

from niki.agents.orchestrator import ChatOrchestrator, ChatResult

router = APIRouter(
    prefix="/chat",
    tags=[  
        {  # type: ignore[list-item]
            "name": "chat",
            "description": "Chat orchestrator API endpoints — the end-to-end pipeline.",
        }
    ]
)

# ISS-08: cache ChatOrchestrator per (child_age, persona) so concurrent
# requests with different ages/personas do not race against the singleton.
# Each (age, persona) combo gets its own instance. Same-combo concurrent
# requests share state, which is fine because the only fields they mutate
# (child_age / persona) are the keys of this cache.
_orchestrator_cache: dict[tuple[int, str], ChatOrchestrator] = {}


def _get_orchestrator(child_age: int = 7, persona: str = "gentle") -> ChatOrchestrator:
    """Return a per-(child_age, persona) ChatOrchestrator (ISS-08).

    Different combos get different instances so each request"s
    `_update_age` / `_update_persona` cannot trample another
    request"s age/persona mid-pipeline.
    """
    key = (child_age, persona)
    if key not in _orchestrator_cache:
        _orchestrator_cache[key] = ChatOrchestrator(child_age=child_age, persona=persona)
    return _orchestrator_cache[key]


def _chat_result_to_dict(result: ChatResult) -> dict:
    """Serialise a ChatResult to JSON."""
    return {
        "final_text": result.final_text,
        "intent": result.intent,
        "primary_agent": result.primary_agent,
        "parallel_agents": result.parallel_agents,
        "is_composite": result.is_composite,
        "safety_alert": result.safety_alert,
        "parent_alert_required": result.parent_alert_required,
        "privacy_tier": result.privacy_tier.value if result.privacy_tier else None,
        "privacy_tier_reason": result.privacy_tier_reason,
        "treehole_engaged": result.treehole_engaged,
        "stages_completed": [s.value for s in result.stages_completed],
        "safety_rewrites": result.safety_rewrites,
        "error": result.error,
        "metadata": result.metadata,
    }


@router.post(
    "/full",
    summary="Run the full chat pipeline end-to-end",
    description="""
            Run the full chat pipeline end-to-end.

            Request body:
                {
                  "text": "...",
                  "child_age": 7,
                  "persona": "gentle" | "lively" | "rigorous",
                  "session_id": "...",
                  "child_id": "...",   # optional; enables HIGH_RISK
                  "parent_id": "..."   # alerts + TREEHOLE_PRIVATE engage
                }
            """,
)
async def chat_full(payload: dict) -> dict:
    """Run the full chat pipeline end-to-end.

    Request body:
        {
          "text": "...",
          "child_age": 7,
          "persona": "gentle" | "lively" | "rigorous",
          "session_id": "...",
          "child_id": "...",   # optional; enables HIGH_RISK alerts
          "parent_id": "..."   # + TREEHOLE_PRIVATE engage
        }
    """
    text = payload.get("text", "")
    raw_age = payload.get("child_age")
    raw_persona = payload.get("persona")
    session_id = payload.get("session_id", "default")
    child_id = payload.get("child_id")
    parent_id = payload.get("parent_id")

    # ISS-08: pick an orchestrator keyed by (age, persona) so concurrent
    # requests with different age/persona do not trample each other.
    effective_age = int(raw_age) if raw_age is not None else 7
    effective_persona = str(raw_persona) if raw_persona is not None else "gentle"
    orchestrator = _get_orchestrator(effective_age, effective_persona)

    result = await orchestrator.chat(
        text=text,
        session_id=session_id,
        child_age=effective_age,
        persona=effective_persona,
        child_id=child_id,
        parent_id=parent_id,
    )
    return _chat_result_to_dict(result)


@router.get(
    "/stages",
    summary="List the stages of the chat pipeline (for debugging)",
    description="List the stages of the chat pipeline (for debugging).",
)
async def list_stages() -> dict:
    """List the stages of the chat pipeline (for debugging)."""
    return {
        "stages": [
            "safety_input",
            "tier_classify",
            "classify",
            "route",
            "dispatch",
            "fuse",
            "safety_output",
            "complete",
        ],
        "agents": [
            "encyclopedia_agent",
            "tutoring_agent",
            "emotional_agent",
        ],
        "personas": ["lively", "gentle", "rigorous"],
        "privacy_tiers": [
            "daily",
            "mild_negative",
            "high_risk",
            "treehole_private",
        ],
    }
