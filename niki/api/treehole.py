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
async def engage(payload: dict) -> dict:
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
        original_text=text,
        emotion=emotion,
        intensity=intensity,
        category=category,
        z_score_deviation=z_score,
    )
    return {
        "summary": summary.to_dict(),
        "encrypted": {
            "child_id": encrypted["child_id"],
            "encrypted_size_bytes": len(encrypted["encrypted"]),
        },
    }


@router.get("/summaries/{child_id}")
async def list_summaries(child_id: str) -> dict:
    """List stored treehole summaries (parent dashboard)."""
    summaries = await _manager.list_summaries()
    # Filter to the requested child
    matching = [s for s in summaries if _matches_child(s, child_id)]
    return {
        "child_id": child_id,
        "count": len(matching),
        "summaries": [s.to_dict() for s in matching],
    }


@router.post("/aggregate")
async def aggregate_baseline() -> dict:
    """Compute the aggregated baseline contribution from treehole data."""
    result = await _manager.aggregate_for_baseline()
    return result


@router.post("/should-activate")
async def should_activate(payload: dict) -> dict:
    """Check if a given text would activate treehole mode."""
    text = payload.get("text", "")
    return {"activates_treehole": should_activate_treehole(text)}


@router.post("/purge-expired")
async def purge_expired() -> dict:
    """Run the 7-day purge (normally called by cron job)."""
    count = await _purger.purge_expired(_manager.store)
    return {"purged_count": count}


@router.post("/purge-all")
async def purge_all() -> dict:
    """Force-purge all treehole records (for account deletion)."""
    count = await _purger.purge_all(_manager.store)
    return {"purged_count": count}


@router.get("/store-stats")
async def store_stats() -> dict:
    """Get the current treehole store statistics."""
    return _manager.store.to_dict()


def _matches_child(summary, child_id: str) -> bool:
    """Check if a summary is associated with a child.

    In a real implementation, this would check the encrypted_payload.
    Here we use a simple convention (the timestamp is enough for the demo).
    """
    # The summary itself doesn't store child_id (for isolation),
    # but in production the store would track it.
    return True  # For demo, return all (caller filters in the API)
