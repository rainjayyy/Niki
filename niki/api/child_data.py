"""Child data API: achievements, chat sessions, weekly report history.

These endpoints persist client-side gamification and history data to
PostgreSQL so it survives browser/localStorage resets and works across
devices. All endpoints require the child to belong to the calling
parent (lateral-auth via get_owned_child_id).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent, get_owned_child_id
from niki.infrastructure.postgres import PostgresClient

router = APIRouter(
    prefix="/child-data",
    tags=[
        {
            "name": "child-data",
            "description": "Persistent child achievements, chat history and weekly report history.",
        }
    ],
)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Achievements
# ---------------------------------------------------------------------------


@router.get(
    "/achievements/{child_id}",
    summary="Get persistent achievements for a child",
    description="Return the child's unlocked achievements and counters from PostgreSQL.",
)
async def get_achievements(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict[str, Any]:
    async for session in PostgresClient.get_session():
        row = (
            (
                await session.execute(
                    text(
                        "SELECT unlocked, counters FROM child_achievements "
                        "WHERE child_id = :child_id"
                    ),
                    {"child_id": child_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return {"child_id": child_id, "unlocked": [], "counters": {}}
        return {
            "child_id": child_id,
            "unlocked": row["unlocked"] or [],
            "counters": row["counters"] or {},
        }


@router.post(
    "/achievements/{child_id}",
    summary="Upsert achievements for a child",
    description="Persist the child's unlocked achievements and counters to PostgreSQL.",
)
async def upsert_achievements(
    payload: dict[str, Any],
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict[str, Any]:
    unlocked = payload.get("unlocked") or []
    counters = payload.get("counters") or {}
    async for session in PostgresClient.get_session():
        await session.execute(
            text(
                "INSERT INTO child_achievements "
                "(child_id, parent_id, unlocked, counters, updated_at) "
                "VALUES (:child_id, :parent_id, CAST(:unlocked AS jsonb), "
                "CAST(:counters AS jsonb), now()) "
                "ON CONFLICT (child_id) DO UPDATE SET "
                "unlocked = EXCLUDED.unlocked, "
                "counters = EXCLUDED.counters, "
                "updated_at = now()"
            ),
            {
                "child_id": child_id,
                "parent_id": parent.id,
                "unlocked": json.dumps(unlocked, ensure_ascii=False),
                "counters": json.dumps(counters, ensure_ascii=False),
            },
        )
        await session.commit()
    return {"child_id": child_id, "unlocked": unlocked, "counters": counters}


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------


@router.get(
    "/chat-sessions/{child_id}",
    summary="List chat sessions for a child",
    description="Return saved chat session metadata for a child from PostgreSQL.",
)
async def list_chat_sessions(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict[str, Any]:
    async for session in PostgresClient.get_session():
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT session_id, child_id, title, started_at, ended_at, "
                        "message_count, duration_sec, topics "
                        "FROM chat_sessions WHERE child_id = :child_id "
                        "ORDER BY ended_at DESC NULLS LAST, updated_at DESC"
                    ),
                    {"child_id": child_id},
                )
            )
            .mappings()
            .all()
        )
        return {
            "child_id": child_id,
            "count": len(rows),
            "sessions": [
                {
                    "id": r["session_id"],
                    "child_id": r["child_id"],
                    "title": r["title"],
                    "started_at": _iso(r["started_at"]),
                    "ended_at": _iso(r["ended_at"]),
                    "message_count": r["message_count"],
                    "duration_sec": r["duration_sec"],
                    "topics": r["topics"] or [],
                }
                for r in rows
            ],
        }


@router.get(
    "/chat-sessions/{child_id}/{session_id}",
    summary="Get a single chat session",
    description="Return one saved chat session including its messages from PostgreSQL.",
)
async def get_chat_session(
    session_id: str,
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict[str, Any]:
    async for session in PostgresClient.get_session():
        row = (
            (
                await session.execute(
                    text(
                        "SELECT session_id, child_id, title, started_at, ended_at, "
                        "message_count, duration_sec, topics, messages "
                        "FROM chat_sessions WHERE child_id = :child_id "
                        "AND session_id = :session_id"
                    ),
                    {"child_id": child_id, "session_id": session_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {
            "id": row["session_id"],
            "child_id": row["child_id"],
            "title": row["title"],
            "started_at": _iso(row["started_at"]),
            "ended_at": _iso(row["ended_at"]),
            "message_count": row["message_count"],
            "duration_sec": row["duration_sec"],
            "topics": row["topics"] or [],
            "messages": row["messages"] or [],
        }


@router.post(
    "/chat-sessions/{child_id}",
    summary="Save or update a chat session",
    description="Persist a chat session (metadata + messages) to PostgreSQL.",
)
async def upsert_chat_session(
    payload: dict[str, Any],
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict[str, Any]:
    session_id = payload.get("id") or payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session id is required")
    started = payload.get("startedAt") or payload.get("started_at")
    ended = payload.get("endedAt") or payload.get("ended_at")
    async for session in PostgresClient.get_session():
        await session.execute(
            text(
                "INSERT INTO chat_sessions "
                "(session_id, child_id, parent_id, title, started_at, ended_at, "
                "message_count, duration_sec, topics, messages, updated_at) "
                "VALUES (:session_id, :child_id, :parent_id, :title, "
                "CAST(:started_at AS timestamptz), CAST(:ended_at AS timestamptz), "
                ":message_count, :duration_sec, CAST(:topics AS jsonb), "
                "CAST(:messages AS jsonb), now()) "
                "ON CONFLICT (session_id) DO UPDATE SET "
                "child_id = EXCLUDED.child_id, "
                "parent_id = EXCLUDED.parent_id, "
                "title = EXCLUDED.title, "
                "started_at = EXCLUDED.started_at, "
                "ended_at = EXCLUDED.ended_at, "
                "message_count = EXCLUDED.message_count, "
                "duration_sec = EXCLUDED.duration_sec, "
                "topics = EXCLUDED.topics, "
                "messages = EXCLUDED.messages, "
                "updated_at = now()"
            ),
            {
                "session_id": session_id,
                "child_id": child_id,
                "parent_id": parent.id,
                "title": payload.get("title") or "",
                "started_at": _parse_dt(started),
                "ended_at": _parse_dt(ended),
                "message_count": int(payload.get("messageCount", 0)),
                "duration_sec": int(payload.get("durationSec", 0)),
                "topics": json.dumps(payload.get("topics") or [], ensure_ascii=False),
                "messages": json.dumps(payload.get("messages") or [], ensure_ascii=False),
            },
        )
        await session.commit()
    return {"session_id": session_id, "status": "saved"}


# ---------------------------------------------------------------------------
# Weekly report history
# ---------------------------------------------------------------------------


@router.get(
    "/weekly-reports/{child_id}",
    summary="List weekly report history",
    description="Return saved weekly report snapshots for a child from PostgreSQL.",
)
async def list_weekly_reports(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict[str, Any]:
    async for session in PostgresClient.get_session():
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id, child_id, week_start, week_end, report, saved_at "
                        "FROM weekly_report_history WHERE child_id = :child_id "
                        "ORDER BY week_start DESC"
                    ),
                    {"child_id": child_id},
                )
            )
            .mappings()
            .all()
        )
        return {
            "child_id": child_id,
            "count": len(rows),
            "reports": [
                {
                    "id": r["id"],
                    "child_id": r["child_id"],
                    "week_start": r["week_start"],
                    "week_end": r["week_end"],
                    "saved_at": _iso(r["saved_at"]),
                    "report": r["report"] or {},
                }
                for r in rows
            ],
        }


@router.post(
    "/weekly-reports/{child_id}",
    summary="Save or update a weekly report snapshot",
    description="Persist a weekly report snapshot to PostgreSQL.",
)
async def upsert_weekly_report(
    payload: dict[str, Any],
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict[str, Any]:
    week_start = payload.get("week_start")
    if not week_start:
        raise HTTPException(status_code=400, detail="week_start is required")
    report_id = payload.get("id") or f"{child_id}__{week_start}"
    week_end = payload.get("week_end") or ""
    report = payload.get("report") or {}
    async for session in PostgresClient.get_session():
        await session.execute(
            text(
                "INSERT INTO weekly_report_history "
                "(id, child_id, week_start, week_end, report, saved_at) "
                "VALUES (:id, :child_id, :week_start, :week_end, "
                "CAST(:report AS jsonb), now()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "week_start = EXCLUDED.week_start, "
                "week_end = EXCLUDED.week_end, "
                "report = EXCLUDED.report, "
                "saved_at = now()"
            ),
            {
                "id": report_id,
                "child_id": child_id,
                "week_start": week_start,
                "week_end": week_end,
                "report": json.dumps(report, ensure_ascii=False),
            },
        )
        await session.commit()
    return {"id": report_id, "status": "saved"}
