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
from niki.schemas.common import ChildInput

router = APIRouter(prefix="/tutoring", tags=["tutoring"])


# A deterministic "LLM" stub for tests + dev: returns the hard-coded prompt
# (so the agent never accidentally gives a direct answer via LLM).
class _StubLLM:
    """Stub LLM that returns the guided-discovery prompt as-is.

    Critical: in the real system, the LLM might try to "leak" the answer
    in rounds 1-3. Our defense-in-depth check (in TutoringAgent) catches
    that and replaces the response with the hard-coded prompt. The stub
    here makes the agent's behavior fully deterministic for tests.
    """

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.6,
        max_tokens: int = 256,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Find the guided prompt in the user message
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # Extract the prompt after the marker
        if "「" in user_msg and "」" in user_msg:
            prompt = user_msg.split("「", 1)[1].split("」", 1)[0]
        else:
            prompt = "我们一起看看这道题~"
        return {"content": prompt}


# Module-level singleton
_agent = TutoringAgent(
    llm=_StubLLM(),
    persona="gentle",
    child_age=7,
)


@router.post("/ask")
async def ask_tutor(payload: dict) -> dict:
    """Ask for help with a stuck problem.

    Request body:
        {
          "problem": "...",
          "child_age": 7,
          "subtype": "pure_calculation" (optional),
          "session_id": "..." (optional, for state continuity)
        }
    """
    problem = payload.get("problem", "")
    child_age = int(payload.get("child_age", 7))
    subtype = payload.get("subtype")

    if child_age != _agent.child_age:
        _agent.child_age = child_age
        _agent._age_group = _agent._resolve_age_group(child_age)
        _agent._output_gateway = _agent._output_gateway.__class__(child_age=child_age)

    child_input = ChildInput(text=problem, session_id="tutoring")
    context: dict[str, Any] = {}
    if subtype:
        context["subtype"] = subtype

    output = await _agent.invoke(child_input, context=context)

    return {
        "text": output.text,
        "intent": output.intent,
        "emotion": output.emotion,
        "metadata": output.metadata,
    }


@router.get("/strategies")
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


@router.get("/difficulty")
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
