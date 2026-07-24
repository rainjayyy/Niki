"""Parent authentication API endpoints — PRD #30, #35, #36.

Demonstration endpoints for the #7 vertical slice:
  POST /auth/signup/start       — begin signup (create PENDING account)
  POST /auth/signup/real-name   — step 1: real-name verification
  POST /auth/signup/face        — step 2: face verification
  POST /auth/signup/consent     — step 3: e-sign consent
  GET  /auth/me/{parent_id}     — get account info
  POST /auth/deletion/request   — request account deletion
  POST /auth/deletion/complete  — complete deletion (after 30 days)
  GET  /auth/consent/document/{type}  — get the consent document text
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from niki.auth import (
    AccountLifecycle,
    AuthError,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Module-level singleton
_lifecycle = AccountLifecycle()


def _error_response(exc: AuthError) -> dict:
    return {"error": exc.code, "message": exc.message}


@router.post("/signup/start")
async def signup_start(payload: dict) -> dict:
    """Begin signup. Creates a PENDING account."""
    try:
        account, progress = await _lifecycle.start_signup(
            name=payload["name"],
            id_number=payload["id_number"],
            phone=payload["phone"],
            email=payload.get("email"),
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Missing field: {exc}") from exc

    return {
        "account": account.to_dict(),
        "progress": {
            "parent_id": progress.parent_id,
            "steps": [
                {"name": s.name, "completed": s.completed}
                for s in progress.steps
            ],
        },
    }


@router.post("/signup/real-name")
async def signup_real_name(payload: dict) -> dict:
    """Step 1: Real-name verification."""
    try:
        account = await _lifecycle.verify_real_name(
            parent_id=payload["parent_id"],
            name=payload["name"],
            id_number=payload["id_number"],
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc

    return {"account": account.to_dict()}


@router.post("/signup/face")
async def signup_face(payload: dict) -> dict:
    """Step 2: Face verification."""
    try:
        account = await _lifecycle.verify_face(
            parent_id=payload["parent_id"],
            live_image_url=payload["live_image_url"],
            reference_image_url=payload.get("reference_image_url"),
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc

    return {"account": account.to_dict()}


@router.post("/signup/consent")
async def signup_consent(payload: dict) -> dict:
    """Step 3: Sign the consent document(s)."""
    try:
        account = await _lifecycle.sign_consent(
            parent_id=payload["parent_id"],
            typed_signature=payload["typed_signature"],
            ip_address=payload.get("ip_address", "0.0.0.0"),
            device_fingerprint=payload.get("device_fingerprint", "unknown"),
            sign_child_consent=payload.get("sign_child_consent", True),
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc

    return {"account": account.to_dict()}


@router.get("/me/{parent_id}")
async def get_account(parent_id: str) -> dict:
    account = await _lifecycle.get_account(parent_id)
    if account is None:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND"})
    progress = await _lifecycle.get_progress(parent_id)
    return {
        "account": account.to_dict(),
        "progress": (
            {
                "steps": [
                    {"name": s.name, "completed": s.completed, "error": s.error}
                    for s in progress.steps
                ]
            }
            if progress
            else None
        ),
    }


@router.post("/deletion/request")
async def deletion_request(payload: dict) -> dict:
    """Request account deletion (starts 30-day window)."""
    parent_id = payload["parent_id"]
    try:
        account = await _lifecycle.request_deletion(parent_id)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc
    return {
        "account": account.to_dict(),
        "deletion_window_remaining_days": account.deletion_window_remaining_days,
    }


@router.post("/deletion/complete")
async def deletion_complete(payload: dict) -> dict:
    """Complete the deletion (after 30 days)."""
    parent_id = payload["parent_id"]
    try:
        account = await _lifecycle.complete_deletion(parent_id)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=_error_response(exc)) from exc
    return {"account": account.to_dict()}


@router.get("/consent/document/{consent_type}")
async def get_consent_document(consent_type: str) -> dict:
    """Get the consent document text (parent or child version)."""
    if consent_type not in ("parent", "child"):
        raise HTTPException(status_code=400, detail={"error": "INVALID_CONSENT_TYPE"})
    consent = _lifecycle.get_consent_manager()
    if consent_type == "parent":
        return {
            "type": "parent",
            "version": consent.PARENT_DOC_VERSION,
            "text": consent.get_parent_document(),
        }
    return {
        "type": "child",
        "version": consent.CHILD_DOC_VERSION,
        "text": consent.get_child_document(),
    }
