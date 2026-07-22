"""Parent dashboard API endpoints — PRD #24-#29.

Demonstration endpoints for the #8 vertical slice:
  GET  /dashboard/weekly-report/{child_id}    — emotion weekly report
  GET  /dashboard/interest/{child_id}        — interest graph
  GET  /dashboard/learning/{child_id}        — learning whitepaper
  GET  /dashboard/alerts/{child_id}          — pending alerts
  POST /dashboard/alerts/record/safety-valve — record a safety-valve event
  POST /dashboard/alerts/record/high-risk    — record a high-risk event
"""

from __future__ import annotations

from fastapi import APIRouter

from niki.agents.emotion import EmotionBaseline, EmotionRecord
from niki.dashboard.service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Module-level singletons
_dashboard = DashboardService()
_baseline = EmotionBaseline()


@router.get("/weekly-report/{child_id}")
async def weekly_report(child_id: str) -> dict:
    """Get the weekly emotion report (PRD #24)."""
    report = _dashboard.generate_weekly_report(
        child_id=child_id,
        baseline=_baseline,
    )
    return report.to_dict()


@router.get("/interest/{child_id}")
async def interest_graph(child_id: str) -> dict:
    """Get the interest graph (PRD #25)."""
    graph = _dashboard.generate_interest_graph(child_id=child_id)
    return graph.to_dict()


@router.get("/learning/{child_id}")
async def learning_whitepaper(child_id: str) -> dict:
    """Get the learning whitepaper (PRD #26)."""
    paper = _dashboard.generate_learning_whitepaper(child_id=child_id)
    return paper.to_dict()


@router.get("/alerts/{child_id}")
async def get_alerts(child_id: str) -> dict:
    """Get pending alerts (PRD #27, #29)."""
    return _dashboard.get_pending_alerts(child_id=child_id)


@router.post("/alerts/record/safety-valve")
async def record_safety_valve(payload: dict) -> dict:
    """Record a tutoring safety-valve trigger."""
    alert = _dashboard.record_safety_valve(
        child_id=payload["child_id"],
        problem_summary=payload["problem_summary"],
        round_reached=int(payload.get("round_reached", 4)),
    )
    return alert.to_dict()


@router.post("/alerts/record/high-risk")
async def record_high_risk(payload: dict) -> dict:
    """Record a high-risk signal (self-harm, etc.)."""
    alert = _dashboard.record_high_risk(
        child_id=payload["child_id"],
        original_text=payload["original_text"],
        niki_response_summary=payload.get(
            "niki_response_summary",
            "Niki 已用温柔的话语回应孩子",
        ),
    )
    return alert.to_dict()


@router.post("/alerts/{alert_id}/delivered")
async def mark_alert_delivered(alert_id: str) -> dict:
    """Mark a high-risk alert as delivered."""
    success = _dashboard.mark_alert_delivered(alert_id)
    return {"alert_id": alert_id, "delivered": success}


@router.post("/baseline/add")
async def add_baseline_record(payload: dict) -> dict:
    """Add a baseline record (for testing/demo)."""
    record = EmotionRecord(
        date=payload["date"],
        valence=float(payload["valence"]),
        intensity=int(payload.get("intensity", 3)),
        source=payload.get("source", "normal"),
    )
    _baseline.add(record)
    return {"status": "added", "record_count": len(_baseline.records())}


@router.post("/events/tutoring")
async def record_tutoring_event(payload: dict) -> dict:
    """Record a tutoring event (for whitepaper generation)."""
    _dashboard.record_tutoring_event(
        child_id=payload["child_id"],
        problem=payload["problem"],
        solved=bool(payload.get("solved", False)),
        safety_valve=bool(payload.get("safety_valve", False)),
    )
    return {"status": "recorded"}


@router.post("/events/knowledge")
async def record_knowledge_query(payload: dict) -> dict:
    """Record a knowledge-base query."""
    _dashboard.record_knowledge_query(
        child_id=payload["child_id"],
        question=payload["question"],
        topic=payload.get("topic", ""),
    )
    return {"status": "recorded"}
