"""Tutoring Agent API endpoints — child-facing homework help.

Demonstration endpoints for the #4 vertical slice:
  POST /tutoring/ask         — ask for help with a stuck problem
  GET  /tutoring/strategies  — list the 4-round guided-discovery strategy
  POST /tutoring/reset       — reset a session state (debugging)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from niki.agents.tutoring import (
    STRATEGY_TEMPLATES,
    SUBTYPE_DIFFICULTY_DISCOUNT,
    Round,
    TutoringAgent,
)
from niki.api._fallback_llm import FallbackLLM
from niki.safety.input_gateway import InputSafetyGateway
from niki.schemas.common import ChildInput

router = APIRouter(
    prefix="/tutoring",
    tags=[  
        {  # type: ignore[list-item]
            "name": "tutoring",
            "description": "Tutoring Agent API endpoints — child-facing homework help.",
        }
    ]
)

# ISS-02: shared input gateway for direct agent endpoints
_input_gateway = InputSafetyGateway()


# Wire the production LLM via the factory — see encyclopedia.py
from niki.infrastructure.llm.factory import (  # noqa: E402
    get_or_create_llm as _get_llm,
    is_fallback_mode as _is_stub,
)

# In stub mode, layer on the domain-aware tutoring stub so dev / tests
# can still exercise the full pipeline.
_llm = FallbackLLM() if _is_stub() else _get_llm()


# Module-level singletons
_agent = TutoringAgent(
    llm=_llm,
    persona="gentle",
    child_age=7,
)

# ISS-08: agent cache keyed by child_age
_agent_cache: dict[int, TutoringAgent] = {}


def _get_agent(child_age: int) -> TutoringAgent:
    """Return a per-child_age Tutoring agent (cached)."""
    if child_age not in _agent_cache:
        _agent_cache[child_age] = TutoringAgent(
            llm=_llm,
            persona="gentle",
            child_age=child_age,
        )
    return _agent_cache[child_age]


@router.post(
    "/ask",
    summary="Ask for help with a stuck problem",
    description="""
            Ask for help with a stuck problem.
            
            Request body:
                {
                  "problem": "...",
                  "child_age": 7,
                  "subtype": "pure_calculation" (optional),
                  "session_id": "..." (optional, for state continuity)
                }
            
            ISS-02: run through InputSafetyGateway first.
            """,
)
async def ask_tutor(payload: dict) -> dict:
    """Ask for help with a stuck problem.

    Request body:
        {
          "problem": "...",
          "child_age": 7,
          "subtype": "pure_calculation" (optional),
          "session_id": "..." (optional, for state continuity)
        }

    ISS-02: run through InputSafetyGateway first.
    """
    problem = payload.get("problem", "")
    child_age = int(payload.get("child_age", 7))
    subtype = payload.get("subtype")

    # ISS-02: safety gate
    safety_verdict = await _input_gateway.check(problem)
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
            "intent": "tutoring",
            "emotion": "caring",
            "metadata": {
                "parent_alert_required": True,
                "escalation": True,
            },
        }

    # ISS-08: use per-child_age agent to avoid concurrent trampling.
    agent = _get_agent(child_age)
    child_input = ChildInput(text=problem, session_id="tutoring")  # type: ignore[call-arg]
    context: dict[str, Any] = {}
    if subtype:
        context["subtype"] = subtype

    output = await agent.invoke(child_input, context=context)

    return {
        "text": output.text,
        "intent": output.intent,
        "emotion": output.emotion,
        "metadata": output.metadata,
    }


@router.get(
    "/strategies",
    summary="List the 4-round guided-discovery strategy templates",
    description="List the 4-round guided-discovery strategy templates.",
)
async def list_strategies() -> dict:
    """List the 4-round guided-discovery strategy templates."""
    return {
        "rounds": [
            {
                "round": int(r),
                "name": {
                    Round.SOCRATIC: "Socratic (open-ended question)",
                    Round.ANALOGY: "Analogy (candies/apples)",
                    Round.VISUAL: "Visual (draw it)",
                    Round.SAFETY_VALVE: "Safety valve (answer + explain)",
                }[r],
                "templates": templates,
            }
            for r, templates in STRATEGY_TEMPLATES.items()
        ],
        "max_attempts": 3,
    }


@router.get(
    "/difficulty",
    summary="Show the per-subtype difficulty discount matrix",
    description="Show the per-subtype difficulty discount matrix.",
)
async def difficulty_matrix() -> dict:
    """Show the per-subtype difficulty discount matrix."""
    return {
        "subtype_discounts": SUBTYPE_DIFFICULTY_DISCOUNT,
        "thresholds": {
            "easy": "effective_difficulty >= 0.7",
            "medium": "0.4 <= effective_difficulty < 0.7",
            "hard": "effective_difficulty < 0.4",
        },
    }
