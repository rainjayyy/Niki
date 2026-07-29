"""Parent dashboard API endpoints — PRD #24-#29.

Demonstration endpoints for the #8 vertical slice:
  GET  /dashboard/weekly-report/{child_id}      — emotion weekly report (JSON)
  GET  /dashboard/weekly-report/{child_id}/pdf — PDF version (BE-DASH-001)
  GET  /dashboard/interest/{child_id}          — interest graph
  GET  /dashboard/learning/{child_id}          — learning whitepaper
  GET  /dashboard/alerts/{child_id}            — pending alerts
  POST /dashboard/alerts/record/safety-valve   — record a safety-valve event
  POST /dashboard/alerts/record/high-risk      — record a high-risk event
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi.responses import Response

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent, get_owned_child_id

from niki.agents.emotion import EmotionBaseline, EmotionRecord
from niki.dashboard.service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Module-level singletons
_dashboard = DashboardService()
_baseline = EmotionBaseline()


@router.get("/weekly-report/{child_id}")
async def weekly_report(
        child_id: str = Depends(get_owned_child_id),
        parent: ParentAccount = Depends(get_current_parent),
    ) -> dict:
    """Get the weekly emotion report (PRD #24)."""
    report = await _dashboard.generate_weekly_report(
        child_id=child_id,
        baseline=_baseline,
    )
    return report.to_dict()


@router.get("/weekly-report/{child_id}/pdf")
async def weekly_report_pdf(
        child_id: str = Depends(get_owned_child_id),
        parent: ParentAccount = Depends(get_current_parent),
    ) -> Response:
    """Get the weekly emotion report as a downloadable PDF (BE-DASH-001).

    Frontend can present this as a single `<a href="…">` link;
    the server does the rendering so the layout is consistent
    across browsers / OSes. A4 portrait, single page for the
    common case.
    """
    # Import here so the API module doesn't pull reportlab
    # unless the endpoint is actually hit.
    from niki.dashboard.pdf import render_weekly_report_pdf

    report = await _dashboard.generate_weekly_report(
        child_id=child_id,
        baseline=_baseline,
    )
    pdf_bytes = render_weekly_report_pdf(
        child_id=child_id, report=report
    )
    # Filename: niki-weekly-c-{short_child_id}-{date}.pdf
    short_id = child_id[:8]
    today = datetime.now().strftime("%Y%m%d")
    filename = f"niki-weekly-c-{short_id}-{today}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Content-Length": str(len(pdf_bytes)),
        },
    )


@router.get("/interest/{child_id}")
async def interest_graph(
        child_id: str = Depends(get_owned_child_id),
        parent: ParentAccount = Depends(get_current_parent),
    ) -> dict:
    """Get the interest graph (PRD #25)."""
    graph = await _dashboard.generate_interest_graph(child_id=child_id)
    return graph.to_dict()


@router.get("/learning/{child_id}")
async def learning_whitepaper(
        child_id: str = Depends(get_owned_child_id),
        parent: ParentAccount = Depends(get_current_parent),
    ) -> dict:
    """Get the learning whitepaper (PRD #26)."""
    paper = await _dashboard.generate_learning_whitepaper(child_id=child_id)
    return paper.to_dict()


@router.get("/alerts/{child_id}")
async def get_alerts(
        child_id: str = Depends(get_owned_child_id),
        parent: ParentAccount = Depends(get_current_parent),
    ) -> dict:
    """Get pending alerts (PRD #27, #29)."""
    pending = await _dashboard.get_pending_alerts(child_id=child_id)
    # Flatten payload fields to top level for legacy client compat.
    def _flatten(row: dict) -> dict:
        flat = {
            "id": row["id"],
            "child_id": row["child_id"],
            "parent_id": row.get("parent_id"),
            "created_at": row.get("created_at").isoformat()
            if row.get("created_at") else None,
            "delivered_at": row.get("delivered_at").isoformat()
            if row.get("delivered_at") else None,
        }
        flat.update(row.get("payload", {}))
        return flat
    return {
        "high_risk": [_flatten(r) for r in pending["high_risk"]],
        "safety_valve": [_flatten(r) for r in pending["safety_valve"]],
    }


@router.post("/alerts/record/safety-valve")
async def record_safety_valve(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Record a tutoring safety-valve trigger."""
    row_id = await _dashboard.record_safety_valve(
        child_id=payload["child_id"],
        parent_id=parent.id,
        problem_summary=payload["problem_summary"],
        round_reached=int(payload.get("round_reached", 4)),
    )
    return {
        "id": row_id,
        "child_id": payload["child_id"],
        "severity": "info",
        "problem_summary": payload["problem_summary"],
        "round_reached": int(payload.get("round_reached", 4)),
    }


@router.post("/alerts/record/high-risk")
async def record_high_risk(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Record a high-risk signal (self-harm, etc.)."""
    niki_response_summary = payload.get(
        "niki_response_summary",
        "Niki 已用温柔的话语回应孩子",
    )
    row_id = await _dashboard.record_high_risk(
        child_id=payload["child_id"],
        parent_id=parent.id,
        original_text=payload["original_text"],
        niki_response_summary=niki_response_summary,
    )
    return {
        "id": row_id,
        "child_id": payload["child_id"],
        "severity": "urgent",
        "original_text": payload["original_text"],
        "niki_response_summary": niki_response_summary,
    }


@router.post("/alerts/{alert_id}/delivered")
async def mark_alert_delivered(alert_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Mark a high-risk alert as delivered."""
    success = await _dashboard.mark_alert_delivered(alert_id)
    return {"alert_id": alert_id, "delivered": success}


@router.post("/baseline/add")
async def add_baseline_record(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Add a baseline record (for testing/demo).

    Persisted via the active EmotionBaselineRepository so the
    7-day rolling window survives process restarts.
    """
    record = EmotionRecord(
        date=payload["date"],
        valence=float(payload["valence"]),
        intensity=int(payload.get("intensity", 3)),
        source=payload.get("source", "normal"),
    )
    await _baseline.add_async(record)
    return {"status": "added", "record_count": len(_baseline.records())}


@router.post("/events/tutoring")
async def record_tutoring_event(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Record a tutoring event (for whitepaper generation)."""
    await _dashboard.record_tutoring_event(
        child_id=payload["child_id"],
        parent_id=parent.id,
        problem=payload["problem"],
        solved=bool(payload.get("solved", False)),
        safety_valve=bool(payload.get("safety_valve", False)),
    )
    return {"status": "recorded"}


@router.post("/events/knowledge")
async def record_knowledge_query(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Record a knowledge-base query."""
    await _dashboard.record_knowledge_query(
        child_id=payload["child_id"],
        parent_id=parent.id,
        question=payload["question"],
        topic=payload.get("topic", ""),
    )
    return {"status": "recorded"}
