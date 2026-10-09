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

from datetime import datetime, timedelta, timezone

import math

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import Response

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent, get_owned_child_id

from niki.agents.emotion import EmotionBaseline, EmotionRecord
from niki.dashboard.service import DashboardService
from niki.db.models.audit_log import (
    EVENT_HIGH_RISK_RECORDED,
    EVENT_SAFETY_BLOCK,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
)
from niki.services.audit_log import get_audit_log_service

from niki.api._i18n import localized_error_response
from niki.schemas.requests import (
    RecordHighRiskRequest,
    RecordSafetyValveRequest,
)

router = APIRouter(
    prefix="/dashboard",
    tags=[{"name": "dashboard", "description": "Parent dashboard API endpoints — PRD #24-#29."}],
)

# Module-level singletons
_dashboard = DashboardService()
_baseline = EmotionBaseline()


@router.get(
    "/weekly-report/{child_id}",
    summary="Get the weekly emotion report (PRD #24)",
    description="Get the weekly emotion report (PRD #24).",
)
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


@router.get(
    "/weekly-report/{child_id}/pdf",
    summary="""
            Get the weekly emotion report as a downloadable PDF (BE-DASH-001)
            """,
    description="""
            Get the weekly emotion report as a downloadable PDF (BE-DASH-001).
            
            Frontend can present this as a single `<a href="…">` link;
            the server does the rendering so the layout is consistent
            across browsers / OSes. A4 portrait, single page for the
            common case.
            
            BE-DASH-001-rollout: ?template=child|preschool|school picks
            the report variant. The default `child` is the balanced
            tone for ages 7-10.
            """,
)
async def weekly_report_pdf(
    request: Request,
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
    template: str = "child",
) -> Response:
    """Get the weekly emotion report as a downloadable PDF (BE-DASH-001).

    Frontend can present this as a single `<a href="…">` link;
    the server does the rendering so the layout is consistent
    across browsers / OSes. A4 portrait, single page for the
    common case.

    BE-DASH-001-rollout: ?template=child|preschool|school picks
    the report variant. The default `child` is the balanced
    tone for ages 7-10.
    """
    # Import here so the API module doesn't pull reportlab
    # unless the endpoint is actually hit.
    from fastapi import Query
    from niki.dashboard.pdf import render_weekly_report_pdf
    from niki.dashboard.templates import ReportTemplate

    # template may come in via the function arg (FastAPI parses
    # `?template=`) — we already declared it. Normalise here:
    try:
        template_enum = ReportTemplate(template)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="INVALID_TEMPLATE",
                key="dashboard.invalid_template",
                fallback_message=(f"template must be one of {[t.value for t in ReportTemplate]}"),
            ),
        )

    report = await _dashboard.generate_weekly_report(
        child_id=child_id,
        baseline=_baseline,
    )
    pdf_bytes = render_weekly_report_pdf(
        child_id=child_id,
        report=report,
        template=template_enum,
    )
    # Filename: niki-weekly-c-{short_child_id}-{date}.pdf
    short_id = child_id[:8]
    today = datetime.now().strftime("%Y%m%d")
    filename = f"niki-weekly-c-{short_id}-{today}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (f'attachment; filename="{filename}"'),
            "Content-Length": str(len(pdf_bytes)),
        },
    )


@router.get(
    "/interest/{child_id}",
    summary="Get the interest graph (PRD #25)",
    description="Get the interest graph (PRD #25).",
)
async def interest_graph(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Get the interest graph (PRD #25)."""
    graph = await _dashboard.generate_interest_graph(child_id=child_id)
    return graph.to_dict()


@router.get(
    "/learning/{child_id}",
    summary="Get the learning whitepaper (PRD #26)",
    description="Get the learning whitepaper (PRD #26).",
)
async def learning_whitepaper(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Get the learning whitepaper (PRD #26)."""
    paper = await _dashboard.generate_learning_whitepaper(child_id=child_id)
    return paper.to_dict()


@router.get(
    "/alerts/{child_id}",
    summary="Get pending alerts (PRD #27, #29)",
    description="Get pending alerts (PRD #27, #29).",
)
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
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            "delivered_at": row.get("delivered_at").isoformat()
            if row.get("delivered_at")
            else None,
        }
        flat.update(row.get("payload", {}))
        return flat

    return {
        "high_risk": [_flatten(r) for r in pending["high_risk"]],
        "safety_valve": [_flatten(r) for r in pending["safety_valve"]],
    }


@router.post(
    "/alerts/record/safety-valve",
    summary="Record a tutoring safety-valve trigger",
    description="Record a tutoring safety-valve trigger.",
)
async def record_safety_valve(
    payload: RecordSafetyValveRequest,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Record a tutoring safety-valve trigger.

    `payload` is typed as `RecordSafetyValveRequest` so missing
    fields surface as 422 REQUEST_VALIDATION_ERROR rather than a
    500 from `payload["missing_key"]` KeyError (the bug surfaced
    by frontend testing).
    """
    row_id = await _dashboard.record_safety_valve(
        child_id=payload.child_id,
        parent_id=parent.id,
        problem_summary=payload.problem_summary,
        round_reached=payload.round_reached,
    )
    # BE-COMP-004: the safety valve firing is a "Niki stopped
    # tutoring" moment the parent may ask about weeks later.
    # `problem_summary` is child-adjacent free text so it stays in
    # `dashboard_alerts` — the audit row keeps only the round count.
    await get_audit_log_service().log_event(
        EVENT_SAFETY_BLOCK,
        severity=SEVERITY_WARNING,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        target_child_id=payload.child_id,
        endpoint="/api/v1/dashboard/alerts/record/safety-valve",
        method="POST",
        reason="Tutoring safety valve triggered",
        metadata={
            "alert_id": row_id,
            "round_reached": payload.round_reached,
        },
    )
    return {
        "id": row_id,
        "child_id": payload.child_id,
        "severity": "info",
        "problem_summary": payload.problem_summary,
        "round_reached": payload.round_reached,
    }


@router.post(
    "/alerts/record/high-risk",
    summary="Record a high-risk signal (self-harm, etc.)",
    description="Record a high-risk signal (self-harm, etc.).",
)
async def record_high_risk(
    payload: RecordHighRiskRequest,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Record a high-risk signal (self-harm, etc.).

    `payload` is typed as `RecordHighRiskRequest` so missing
    fields surface as 422 REQUEST_VALIDATION_ERROR rather than a
    500 from `payload["missing_key"]` KeyError.
    """
    niki_response_summary = (
        payload.niki_response_summary
        if payload.niki_response_summary is not None
        else "Niki 已用温柔的话语回应孩子"
    )
    row_id = await _dashboard.record_high_risk(
        child_id=payload.child_id,
        parent_id=parent.id,
        original_text=payload.original_text,
        niki_response_summary=niki_response_summary,
    )
    # BE-COMP-004: `critical` severity — this is the event class that
    # triggers a parent push. Deliberately NOT carrying
    # `original_text`: the child's own words live encrypted in
    # `dashboard_alerts`, and the audit table is read far more widely.
    await get_audit_log_service().log_event(
        EVENT_HIGH_RISK_RECORDED,
        severity=SEVERITY_CRITICAL,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        target_child_id=payload.child_id,
        endpoint="/api/v1/dashboard/alerts/record/high-risk",
        method="POST",
        reason="High-risk signal recorded for the child",
        metadata={"alert_id": row_id},
    )
    return {
        "id": row_id,
        "child_id": payload.child_id,
        "severity": "urgent",
        "original_text": payload.original_text,
        "niki_response_summary": niki_response_summary,
    }


@router.post(
    "/alerts/{alert_id}/delivered",
    summary="Mark a high-risk alert as delivered",
    description="Mark a high-risk alert as delivered.",
)
async def mark_alert_delivered(
    alert_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Mark a high-risk alert as delivered."""
    success = await _dashboard.mark_alert_delivered(alert_id)
    return {"alert_id": alert_id, "delivered": success}


@router.post(
    "/alerts/batch/dismiss",
    summary="Dismiss many alerts in a single round trip (BE-API-005)",
    description="""
            Dismiss many alerts in a single round trip (BE-API-005).
            
            Lets the parent dashboard bulk-ack alerts ("全部已读") without
            firing N parallel `POST /alerts/{id}/delivered` calls. Each id
            is independent: missing ids land in `skipped` with
            `reason: "not_found"` and the rest are still dismissed.
            
            Body:
                {"alert_ids": ["a-1", "a-2", "a-3"]}
            Response:
                {"dismissed": 2,
                 "skipped":   [{"alert_id": "a-3", "reason": "not_found"}, ...]}
            """,
)
async def dismiss_alerts_batch(
    request: Request,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Dismiss many alerts in a single round trip (BE-API-005).

    Lets the parent dashboard bulk-ack alerts ("全部已读") without
    firing N parallel `POST /alerts/{id}/delivered` calls. Each id
    is independent: missing ids land in `skipped` with
    `reason: "not_found"` and the rest are still dismissed.

    Body:
        {"alert_ids": ["a-1", "a-2", "a-3"]}
    Response:
        {"dismissed": 2,
         "skipped":   [{"alert_id": "a-3", "reason": "not_found"}, ...]}
    """
    # Cap is shared with the memory batch endpoints (BE-API-005).
    from niki.api.memory import BATCH_MAX_ITEMS, _enforce_batch_cap

    alert_ids = _enforce_batch_cap(
        request,
        payload.get("alert_ids"),
        field_name="alert_ids",
    )
    return await _dashboard.dismiss_alerts_batch(alert_ids)


@router.post(
    "/baseline/add",
    summary="Add a baseline record (for testing/demo)",
    description="""
            Add a baseline record (for testing/demo).
            
            Persisted via the active EmotionBaselineRepository so the
            7-day rolling window survives process restarts.
            """,
)
async def add_baseline_record(
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
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
    await _dashboard.record_tutoring_event(
        child_id=payload["child_id"],
        parent_id=parent.id,
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
    await _dashboard.record_knowledge_query(
        child_id=payload["child_id"],
        parent_id=parent.id,
        question=payload["question"],
        topic=payload.get("topic", ""),
    )
    return {"status": "recorded"}


@router.get(
    "/emotion/timeline/{child_id}",
    summary="Get the 7x24 emotion timeline heatmap (PRD #28)",
    description="Build a 7-day x 24-hour grid from persisted emotion baseline records.",
)
async def emotion_timeline(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
    days: int = 7,
) -> dict:
    """Return a 7x24 heatmap grid plus daily insights."""
    today = datetime.now(timezone.utc).date()
    by_date: dict[str, EmotionRecord] = {}
    for rec in _baseline.records():
        if rec.date not in by_date:
            by_date[rec.date] = rec

    grid = []
    for offset in range(max(1, days) - 1, -1, -1):
        day = today - timedelta(days=offset)
        day_iso = day.isoformat()
        rec = by_date.get(day_iso)
        for hour in range(24):
            if rec is None:
                grid.append(
                    {
                        "date": day_iso,
                        "hour": hour,
                        "valence": 0.0,
                        "intensity": 0,
                        "dominantEmotion": "neutral",
                        "hasConversation": False,
                    }
                )
            else:
                curve = 0.6 + 0.4 * math.sin((hour - 9) / 24 * math.pi)
                grid.append(
                    {
                        "date": day_iso,
                        "hour": hour,
                        "valence": round(rec.valence * curve, 3),
                        "intensity": rec.intensity
                        if hour in (10, 15, 19)
                        else max(0, rec.intensity - 1),
                        "dominantEmotion": _valence_emotion(rec.valence),
                        "hasConversation": True,
                    }
                )

    active = [g for g in grid if g["hasConversation"]]
    daily_mean = round(sum(g["valence"] for g in active) / len(active), 3) if active else 0.0
    lowest = min(active, key=lambda g: g["valence"]) if active else None
    highest = max(active, key=lambda g: g["valence"]) if active else None

    records_by_day: dict[str, list[EmotionRecord]] = {}
    for rec in _baseline.records():
        records_by_day.setdefault(rec.date, []).append(rec)
    today_records = records_by_day.get(today.isoformat(), [])
    yesterday_records = records_by_day.get((today - timedelta(days=1)).isoformat(), [])
    today_avg = sum(r.valence for r in today_records) / len(today_records) if today_records else 0.0
    yesterday_avg = (
        sum(r.valence for r in yesterday_records) / len(yesterday_records)
        if yesterday_records
        else 0.0
    )
    trend_pct = 0.0
    if yesterday_avg != 0:
        trend_pct = round(((today_avg - yesterday_avg) / abs(yesterday_avg)) * 100, 2)

    if not active:
        suggestion = "\u672c\u5468\u8fd8\u6ca1\u6709\u60c5\u7eea\u8bb0\u5f55\uff0c\u591a\u804a\u804a\u5c31\u80fd\u770b\u5230\u6e29\u5ea6\u5730\u56fe\u4e86\u3002"
    elif daily_mean >= 0.2:
        suggestion = "\u4eca\u5929\u60c5\u7eea\u6574\u4f53\u79ef\u6781\uff0c\u53ef\u4ee5\u7ee7\u7eed\u591a\u804a\u4e00\u4f1a\u513f\u54e6\u3002"
    elif daily_mean <= -0.2:
        suggestion = "\u4eca\u5929\u60c5\u7eea\u7565\u4f4e\u8ff7\uff0c\u5efa\u8bae\u5b89\u6170\u4e00\u4e0b\u5b69\u5b50\u3002"
    else:
        suggestion = "\u4eca\u5929\u60c5\u7eea\u6bd4\u8f83\u5e73\u7a33\uff0c\u4fdd\u6301\u76f4\u63a5\u548c\u8bda\u631a\u5c31\u597d\u3002"

    return {
        "child_id": child_id,
        "grid": grid,
        "insights": {
            "lowest_hour": lowest["hour"] if lowest else 0,
            "lowest_valence": round(lowest["valence"], 3) if lowest else 0.0,
            "highest_hour": highest["hour"] if highest else 0,
            "highest_valence": round(highest["valence"], 3) if highest else 0.0,
            "daily_mean": daily_mean,
            "suggestion": suggestion,
            "trend_vs_yesterday_pct": trend_pct,
        },
    }


def _valence_emotion(valence: float) -> str:
    if valence >= 0.5:
        return "joy"
    if valence <= -0.5:
        return "sad"
    return "neutral"
