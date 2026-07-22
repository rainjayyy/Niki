"""Emotion Agent API endpoints — child-facing emotional support.

Demonstration endpoints for the #5 vertical slice:
  POST /emotion/express     — child shares an emotion
  GET  /emotion/baseline   — view baseline statistics
  POST /emotion/proactive-care — run 8pm proactive care check
  GET  /emotion/good-night — short good-night greeting
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from niki.agents.emotion import (
    EmotionAgent,
    EmotionBaseline,
    EmotionRecord,
)
from niki.schemas.common import ChildInput

router = APIRouter(prefix="/emotion", tags=["emotion"])


# A deterministic "LLM" stub for tests + dev.
class _StubLLM:
    """Stub LLM that returns the canned empathy template (no real call)."""

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.5,
        max_tokens: int = 256,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # In real life, this would be a DeepSeek-V4 reply.
        return {"content": "Niki听到你了~"}


# Module-level singletons
_baseline = EmotionBaseline()
_agent = EmotionAgent(
    llm=_StubLLM(),
    baseline=_baseline,
    persona="gentle",
    child_age=7,
)


@router.post("/express")
async def express_emotion(payload: dict) -> dict:
    """Child shares an emotional expression.

    Request body: {"text": "...", "child_age": 7}
    """
    text = payload.get("text", "")
    child_age = int(payload.get("child_age", 7))

    if child_age != _agent.child_age:
        _agent.child_age = child_age
        _agent._age_group = _agent._resolve_age_group(child_age)
        _agent._output_gateway = _agent._output_gateway.__class__(child_age=child_age)

    child_input = ChildInput(text=text, session_id="emotion")
    output = await _agent.invoke(child_input)

    return {
        "text": output.text,
        "intent": output.intent,
        "emotion": output.emotion,
        "metadata": output.metadata,
    }


@router.get("/baseline")
async def get_baseline() -> dict:
    """View the current 7-day baseline statistics."""
    stats = _baseline.window_stats()
    return {
        "window_days": stats.window_days,
        "mean": stats.mean,
        "std": stats.std,
        "sample_count": stats.sample_count,
        "z_score_threshold": stats.z_score_threshold,
        "in_calibration": _baseline.is_in_calibration(),
        "records": [
            {
                "date": r.date,
                "valence": r.valence,
                "intensity": r.intensity,
                "source": r.source,
            }
            for r in _baseline.records()
        ],
    }


@router.post("/proactive-care")
async def proactive_care_check() -> dict:
    """Run the 8pm proactive-care check (PRD #12)."""
    care = _agent.run_proactive_care()
    if care is None:
        return {
            "should_care": False,
            "reason": "no anomaly detected (or in calibration)",
        }
    return {
        "should_care": True,
        "child_id": care.child_id,
        "scheduled_at": care.scheduled_at.isoformat(),
        "today_valence": care.today_valence,
        "anomaly": {
            "is_anomaly": care.anomaly.is_anomaly,
            "z_score": care.anomaly.z_score,
            "direction": care.anomaly.direction,
            "reason": care.anomaly.reason,
        },
    }


@router.get("/good-night")
async def good_night() -> dict:
    """Short good-night greeting (PRD #16)."""
    output = _agent.good_night()
    return {
        "text": output.text,
        "metadata": output.metadata,
    }


@router.post("/baseline/add")
async def add_baseline_record(payload: dict) -> dict:
    """Add a record to the baseline (for testing/demo)."""
    record = EmotionRecord(
        date=payload["date"],
        valence=float(payload["valence"]),
        intensity=int(payload.get("intensity", 3)),
        source=payload.get("source", "normal"),
    )
    _baseline.add(record)
    return {"status": "added", "record_count": len(_baseline.records())}
