"""Chat orchestrator API endpoints — the end-to-end pipeline.

Demonstration endpoint for the #6 vertical slice:
  POST /chat/full — run the full safety + classify + route + dispatch + fuse + safety pipeline

This is the integration test of all previous slices (#1 safety,
#2 routing+persona, #3 encyclopedia, #4 tutoring, #5 emotion).
"""

from __future__ import annotations

from fastapi import APIRouter

from niki.agents.orchestrator import ChatOrchestrator, ChatResult

router = APIRouter(prefix="/chat", tags=[
    {"name": "chat", "description": "Chat orchestrator API endpoints — the end-to-end pipeline."}
])

# Module-level singleton (default config: 7-year-old, gentle persona)
_orchestrator = ChatOrchestrator(child_age=7, persona="gentle")


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
                  "session_id": "..."
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
          "session_id": "..."
        }
    """
    text = payload.get("text", "")
    child_age = payload.get("child_age")
    persona = payload.get("persona")
    session_id = payload.get("session_id", "default")

    result = await _orchestrator.chat(
        text=text,
        session_id=session_id,
        child_age=child_age,
        persona=persona,
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
    }
