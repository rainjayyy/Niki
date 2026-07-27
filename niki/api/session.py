"""Session control API endpoints — PRD #17, #23, #31, #33.

Demonstration endpoints for #12 vertical slice:
  POST /session/start                     — start a new session
  POST /session/activity                  — record activity (reset idle)
  GET  /session/stats/{child_id}          — current session stats
  GET  /session/should-warn/{child_id}    — check soft warning
  GET  /session/is-exhausted/{child_id}   — check hard limit
  GET  /session/is-idle/{child_id}        — check idle timeout (PRD #17)
  POST /session/end                       — natural exit (child bye / limit)
  POST /session/parent/pause              — parent one-tap pause (PRD #33)
  GET  /session/parent/has-pause/{child_id} — check pending pause
  POST /session/parent/consume-pause      — consume pause + natural exit
  GET  /session/parent/paused/{child_id}  — check if paused
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent

from niki.session import (
    ExitReason,
    RemotePauseController,
    SessionLimiter,
)

router = APIRouter(prefix="/session", tags=["session"])

# Module-level singletons
_limiter = SessionLimiter()
_pause = RemotePauseController(_limiter)


@router.post("/start")
async def start_session(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Start a new session for a child."""
    child_id = payload.get("child_id", "")
    _limiter.start_session(child_id)
    return {"child_id": child_id, "status": "started"}


@router.post("/activity")
async def record_activity(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Record activity (resets idle timer)."""
    child_id = payload.get("child_id", "")
    _limiter.record_activity(child_id)
    return {"child_id": child_id, "status": "active"}


@router.get("/stats/{child_id}")
async def get_stats(child_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Get current session stats."""
    return {
        "child_id": child_id,
        "is_started": _limiter.is_started(child_id),
        "elapsed_minutes": round(_limiter.elapsed_minutes(child_id), 2),
        "idle_minutes": round(_limiter.idle_minutes(child_id), 2),
        "remaining_minutes": round(_limiter.get_remaining_minutes(child_id), 2),
        "soft_warn_due": _limiter.should_warn(child_id),
        "is_exhausted": _limiter.is_exhausted(child_id),
        "is_idle_timeout": _limiter.is_idle_timeout(child_id),
        "daily_limit_minutes": _limiter.daily_limit_minutes,
    }


@router.get("/should-warn/{child_id}")
async def should_warn(child_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Check if the soft warning should be issued (5 min before hard limit)."""
    return {
        "should_warn": _limiter.should_warn(child_id),
        "remaining_minutes": round(_limiter.get_remaining_minutes(child_id), 2),
    }


@router.get("/is-exhausted/{child_id}")
async def is_exhausted(child_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Check if the hard daily limit is reached."""
    return {
        "is_exhausted": _limiter.is_exhausted(child_id),
        "elapsed_minutes": round(_limiter.elapsed_minutes(child_id), 2),
    }


@router.get("/is-idle/{child_id}")
async def is_idle(child_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Check if the idle timeout is reached (PRD #17)."""
    return {
        "is_idle_timeout": _limiter.is_idle_timeout(child_id),
        "idle_minutes": round(_limiter.idle_minutes(child_id), 2),
    }


@router.post("/end")
async def end_session(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """End a session naturally (child says bye, or hard limit reached)."""
    child_id = payload.get("child_id", "")
    reason_str = payload.get("reason", "child_bye")
    try:
        reason = ExitReason(reason_str)
    except ValueError:
        reason = ExitReason.CHILD_BYE

    stats = _limiter.end_session(child_id, reason=reason)
    if stats is None:
        return {"error": "no session to end"}
    return {"stats": stats.to_dict()}


# ---- Parent pause endpoints (PRD #33) ----


@router.post("/parent/pause")
async def parent_pause(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Record a pause request from the parent's mini-program.

    The next time the child sends a message, Niki will end the
    session with a natural exit message (so the child doesn't
    know the parent intervened).
    """
    child_id = payload.get("child_id", "")
    parent_id = payload.get("parent_id", "")
    _pause.request_pause(child_id, parent_id)
    return {"status": "pause_queued"}


@router.get("/parent/has-pause/{child_id}")
async def has_pause(child_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Check if a parent pause is pending for the child."""
    return {"has_pending_pause": _pause.has_pending_pause(child_id)}


@router.post("/parent/consume-pause")
async def consume_pause(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Consume a pending pause (returns natural exit message)."""
    child_id = payload.get("child_id", "")
    msg = _pause.consume_pause(child_id)
    if msg is None:
        return {"error": "no pending pause"}
    return {"exit_message": msg.to_dict()}


@router.post("/parent/end-session")
async def parent_end_session(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """End a session as a parent-pause (with natural exit message)."""
    child_id = payload.get("child_id", "")
    topics = payload.get("topics_visited", [])
    stats = _pause.end_session_by_parent(child_id, topics_visited=topics)
    if stats is None:
        return {"error": "no session to end"}
    msg = _pause.build_natural_exit(child_id, topics)
    return {"stats": stats.to_dict(), "exit_message": msg.to_dict()}


@router.get("/parent/paused/{child_id}")
async def is_paused(child_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """True if the child has been paused (any reason)."""
    return {"is_paused": _pause.is_paused(child_id)}


@router.post("/parent/clear-pause")
async def clear_pause(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Clear the pause state (when parent resumes)."""
    child_id = payload.get("child_id", "")
    _pause.clear_pause(child_id)
    return {"status": "cleared"}
