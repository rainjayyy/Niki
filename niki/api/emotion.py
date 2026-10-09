"""Emotion Agent API endpoints — child-facing emotional support.

Demonstration endpoints for the #5 vertical slice:
  POST /emotion/express     — child shares an emotion
  GET  /emotion/baseline   — view baseline statistics
  POST /emotion/proactive-care — run 8pm proactive care check
  GET  /emotion/good-night — short good-night greeting
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent

from niki.agents.emotion import (
    EMPATHY_TEMPLATES,
    EmotionAgent,
    EmotionBaseline,
    EmotionCategory,
    EmotionRecord,
    should_activate_treehole,
)
from niki.api._stub_llm import _StubLLM
from niki.dashboard.service import DashboardService
from niki.safety.input_gateway import InputSafetyGateway
from niki.schemas.common import ChildInput

router = APIRouter(
    prefix="/emotion",
    tags=[
        {
            "name": "emotion",
            "description": ("Emotion Agent API endpoints — child-facing emotional support."),
        },
    ],
)

# ISS-02: a shared input gateway so self-harm / blacklisted topics
# trigger parent alert even when calling the agent directly (not via /chat).
_input_gateway = InputSafetyGateway()


# Module-level singletons
_dashboard = DashboardService()
_baseline = EmotionBaseline()

# Wire the production LLM via the factory — see encyclopedia.py
from niki.infrastructure.llm.factory import (  # noqa: E402
    get_or_create_llm as _get_llm,
    is_stub_mode as _is_stub,
)

# In stub mode, layer on the generic stub so dev / tests can still
# exercise the pipeline without network access.
_llm = _StubLLM() if _is_stub() else _get_llm()

_orchestrator = EmotionAgent(
    llm=_llm,
    baseline=_baseline,
    persona="gentle",
    child_age=7,
)


@router.post(
    "/express",
    summary="Child shares an emotional expression",
    description="""
            Child shares an emotional expression.
            
            Request body: {"text": "...", "child_age": 7}
            
            ISS-02: run the text through InputSafetyGateway first so that
            self-harm / blacklisted topics trigger the same parent alert
            as the orchestrator path.
            """,
)
async def express_emotion(
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Child shares an emotional expression.

    Request body: {"text": "...", "child_age": 7}

    ISS-02: run the text through InputSafetyGateway first so that
    self-harm / blacklisted topics trigger the same parent alert
    as the orchestrator path.
    """
    text = payload.get("text", "")
    child_age = int(payload.get("child_age", 7))

    # ISS-02: safety gate before agent dispatch
    safety_verdict = await _input_gateway.check(text)
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
            "intent": "emotional",
            "emotion": "caring",
            "metadata": {
                "parent_alert_required": True,
                "escalation": True,
            },
        }

    # Fast rule-based classification keeps this endpoint lightweight:
    # the chat orchestrator already produces the full empathetic reply.
    emotion = EmotionAgent._detect_emotion(text)
    intensity = EmotionAgent._compute_intensity(text, emotion)
    valence = EmotionAgent._emotion_to_valence(emotion, intensity)
    anomaly = _baseline.check_anomaly(valence) if emotion != EmotionCategory.NEUTRAL else None

    return {
        "text": EMPATHY_TEMPLATES.get(emotion, "Niki?????~"),
        "intent": "emotional",
        "emotion": emotion.value,
        "metadata": {
            "intensity": intensity,
            "rule_based": True,
            "treehole_activated": should_activate_treehole(text),
            "anomaly": {
                "is_anomaly": anomaly.is_anomaly,
                "z_score": anomaly.z_score,
                "direction": anomaly.direction,
            }
            if anomaly
            else None,
        },
    }


@router.get(
    "/baseline",
    summary="Get baseline statistics",
    description="Get baseline statistics.",
)
async def get_baseline(
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Get baseline statistics."""
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


@router.post(
    "/proactive-care",
    summary="Run the 8pm proactive-care check (PRD #12)",
    description="Run the 8pm proactive-care check (PRD #12).",
)
async def proactive_care_check(
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Run the 8pm proactive-care check (PRD #12)."""
    care = _orchestrator.run_proactive_care()
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


@router.get(
    "/good-night",
    summary="Short good-night greeting (PRD #16)",
    description="Short good-night greeting (PRD #16).",
)
async def good_night(
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Short good-night greeting (PRD #16)."""
    output = _orchestrator.good_night()
    return {
        "text": output.text,
        "metadata": output.metadata,
    }


@router.post(
    "/baseline/add",
    summary="Add a record to the baseline (for testing/demo)",
    description="Add a record to the baseline (for testing/demo).",
)
async def add_baseline_record(
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Add a record to the baseline (for testing/demo)."""
    record = EmotionRecord(
        date=payload["date"],
        valence=float(payload["valence"]),
        intensity=int(payload.get("intensity", 3)),
        source=payload.get("source", "normal"),
    )
    _baseline.add(record)
    return {"status": "added", "record_count": len(_baseline.records())}


@router.post(
    "/events/tutoring",
    summary="Record a tutoring event (for whitepaper generation)",
    description="Record a tutoring event (for whitepaper generation).",
)
async def record_tutoring_event(
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Record a tutoring event (for whitepaper generation)."""
    _dashboard.record_tutoring_event(
        child_id=payload["child_id"],
        problem=payload["problem"],
        solved=bool(payload.get("solved", False)),
        safety_valve=bool(payload.get("safety_valve", False)),
    )
    return {"status": "recorded"}


@router.post(
    "/events/knowledge",
    summary="Record a knowledge-base query",
    description="Record a knowledge-base query.",
)
async def record_knowledge_query(
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Record a knowledge-base query."""
    _dashboard.record_knowledge_query(
        child_id=payload["child_id"],
        question=payload["question"],
        topic=payload.get("topic", ""),
    )
    return {"status": "recorded"}
