"""Treehole API endpoints — PRD #13, #61-#64.

Demonstration endpoints for the #11 vertical slice:
  POST /treehole/engage               — engage treehole mode for a child message
  GET  /treehole/summaries/{child_id}  — list stored summaries (parent dashboard)
  POST /treehole/aggregate            — compute aggregated baseline contribution
  GET  /treehole/should-activate      — check if text triggers treehole mode
  POST /treehole/purge-expired        — run the 7-day purge (admin/cron)
  POST /treehole/purge-all            — force purge all (account deletion)
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException

from niki.api._pagination import (
    ListQueryParams,
    list_query_params,
    paginate,
    parse_pagination,
)
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent, get_owned_child_id

from niki.treehole import (
    TreeholeManager,
    TreeholePurger,
    should_activate_treehole,
)

router = APIRouter(prefix="/treehole", tags=["treehole"])

# Module-level singletons
_manager = TreeholeManager()
_purger = TreeholePurger()


@router.post("/engage")
async def engage(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Engage treehole mode for a child message.

    Request body:
        {
          "child_id": "...",
          "text": "...",         # NEVER stored
          "emotion": "negative",  # optional, default neutral
          "intensity": 3,         # 1-5
          "category": "other"     # family | school | self | other
        }
    """
    child_id = payload.get("child_id", "")
    text = payload.get("text", "")
    emotion = payload.get("emotion", "neutral")
    intensity = int(payload.get("intensity", 3))
    category = payload.get("category", "other")
    z_score = payload.get("z_score_deviation")

    summary, encrypted = await _manager.engage(
        child_id=child_id,
        parent_id=parent.id,
        original_text=text,
        emotion=emotion,
        intensity=intensity,
        category=category,
        z_score_deviation=z_score,
    )
    return {
        "summary": summary.to_dict(),
        "encrypted": {
            "row_id": encrypted["id"],
            "child_id": encrypted["child_id"],
            "encrypted_size_bytes": len(encrypted["encrypted"]),
        },
    }


@router.get("/summaries/{child_id}")
async def list_summaries(
        child_id: str = Depends(get_owned_child_id),
        parent: ParentAccount = Depends(get_current_parent),
        pagination: tuple[int, int] = Depends(parse_pagination),
        params: ListQueryParams = Depends(list_query_params),
    ) -> dict:
    """List stored treehole summaries (parent dashboard, BE-API-003 paginated).

    BE-API-004: optional ?since=&until=&keyword=&sort=&order= filter
    the summary list server-side. Default sort is `timestamp`
    desc (newest first). Allowed sort fields: timestamp,
    emotion_intensity.
    """
    page, page_size = pagination
    summaries = await _manager.list_summaries(
        child_id=child_id,
        since=params.since,
        until=params.until,
        sort=params.sort,
        order=params.order,
        keyword=params.keyword,
    )
    payload = paginate(
        [s.to_dict() for s in summaries],
        page,
        page_size,
        items_key="summaries",
    )
    # Back-compat with the pre-pagination shape.
    return {
        "child_id": child_id,
        "count": payload["total"],
        **payload,
    }


@router.post("/aggregate")
async def aggregate_baseline(payload: dict | None = None,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Compute the aggregated baseline contribution from treehole data.

    Optional body:
        child_id   — scope to a child
        window     — "daily" | "weekly" (BE-TREEHOLE-AGG-rollout);
                    default "weekly" (back-compat)
    """
    payload = payload or {}
    child_id = payload.get("child_id")
    window_kind = payload.get("window", "weekly")
    if window_kind not in ("daily", "weekly"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_WINDOW",
                "message": "window must be 'daily' or 'weekly'",
            },
        )
    result = await _manager.aggregate_for_baseline(
        child_id=child_id, window_kind=window_kind
    )
    return result


@router.post("/should-activate")
async def should_activate(payload: dict | None = None,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Check if a given text would activate treehole mode."""
    payload = payload or {}
    text = payload.get("text", "")
    return {"activates_treehole": should_activate_treehole(text)}


@router.post("/purge-expired")
async def purge_expired(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Run the 7-day purge (normally called by cron job)."""
    count = await _purger.purge_expired()
    return {"purged_count": count}


@router.post("/purge-all")
async def purge_all(payload: dict | None = None,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Force-purge treehole records. Optional body: {"child_id": "..."}.

    Without child_id, purges ALL records (debug/test only). With
    child_id, purges only that child's (account deletion path).
    """
    payload = payload or {}
    child_id = payload.get("child_id")
    if child_id:
        count = await _manager.purge_all_for_child(child_id)
    else:
        count = await _purger.purge_all()
    return {"purged_count": count}


@router.get("/store-stats")
async def store_stats(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Get the current treehole repository statistics."""
    from niki.db.repositories import InMemoryTreeholeRepository

    rows = await _manager.repo.list_all()
    return {
        "summary_count": len(rows),
        "in_memory_repo": isinstance(_manager.repo, InMemoryTreeholeRepository),
    }


def _matches_child(summary, child_id: str) -> bool:
    """Legacy helper — kept for any external imports."""
    return True
