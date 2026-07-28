"""Children API endpoints (BE-STOR-001-ext-E).

POST /api/v1/parents/{parent_id}/children
  - Registers a child under the calling parent.
  - Lateral-auth check: the parent_id in the path must match the
    token's parent.id (403 PARENT_MISMATCH otherwise), and the child
    cannot already be owned by another parent (409 CHILD_OWNED).

GET /api/v1/parents/{parent_id}/children
  - Lists child_ids owned by the calling parent.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends, HTTPException, Request

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.services.child_registry import get_child_registry

router = APIRouter(prefix="/children", tags=["children"])


@router.post("/register/{parent_id}")
async def register_child(
    parent_id: str,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Register a child under a parent.

    Body: {"child_id": "<uuid>", "name": "<optional>"}

    Authorization: the path parent_id must equal the token's
    parent.id; the child_id must not already be owned by another
    parent.
    """
    if parent.id != parent_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PARENT_MISMATCH",
                "message": "Token does not own this parent_id",
            },
        )

    child_id = payload.get("child_id")
    if not child_id or not isinstance(child_id, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "BAD_REQUEST", "message": "child_id is required"},
        )
    name = payload.get("name", "(child)")

    registry = get_child_registry()
    existing = registry.get_parent_of(child_id)
    if existing is not None and existing != parent_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "CHILD_OWNED",
                "message": f"child {child_id!r} already owned by another parent",
            },
        )
    registry.register(child_id, parent_id, name=name)
    return {
        "child_id": child_id,
        "parent_id": parent_id,
        "name": name,
        "status": "registered",
    }


@router.get("/list/{parent_id}")
async def list_children(
    parent_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """List child_ids owned by the calling parent."""
    if parent.id != parent_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PARENT_MISMATCH",
                "message": "Token does not own this parent_id",
            },
        )
    child_ids = get_child_registry().list_children_of(parent_id)
    return {"parent_id": parent_id, "count": len(child_ids), "child_ids": child_ids}
