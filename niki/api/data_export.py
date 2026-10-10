"""Data export API endpoint (BE-COMP-001)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.services.data_export import build_export_zip

router = APIRouter(
    prefix="/parents",
    tags=[{"name": "data-portability", "description": "Data export API endpoint (BE-COMP-001)."}]  # type: ignore[list-item]
)


@router.get(
    "/{parent_id}/data-export",
    summary="Download a ZIP of all of a parent's data (PIPL Art",
    description="""
            Download a ZIP of all of a parent's data (PIPL Art. 44).
            
            The token's `sub` claim must match `parent_id` (lateral-isolation
            guard). Returns 403 PARENT_MISMATCH otherwise.
            
            Optional `child_id` filters the export to a single child.
            """,
)
async def export_data(
    parent_id: str,
    child_id: str | None = Query(default=None),
    parent: ParentAccount = Depends(get_current_parent),
) -> Response:
    """Download a ZIP of all of a parent's data (PIPL Art. 44).

    The token's `sub` claim must match `parent_id` (lateral-isolation
    guard). Returns 403 PARENT_MISMATCH otherwise.

    Optional `child_id` filters the export to a single child.
    """
    if parent.id != parent_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PARENT_MISMATCH",
                "message": "Token does not own this parent_id",
            },
        )

    try:
        zip_bytes, summary = await build_export_zip(parent_id=parent_id, child_id=child_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "EXPORT_UNAVAILABLE",
                "message": str(exc),
            },
        ) from exc

    filename = "niki_data_export_" + parent_id + "_" + summary.exported_at.split("T")[0] + ".zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="' + filename + '"',
            "X-Export-Record-Count": str(summary.total_records),
            "X-Export-Size-Bytes": str(summary.zipped_size),
        },
    )
