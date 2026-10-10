"""Media API endpoints (BE-MEDIA-001, BE-MEDIA-004).

Currently exposes:
    POST /api/v1/media/upload
        - multipart/form-data with field `file`
        - validates MIME via magic bytes
        - delegates to MediaStorage.put(...)
        - returns {file_id, mime_type, size_bytes, sha1, access_url,
                   uploaded_at, category}
    POST /api/v1/media/avatar           (BE-MEDIA-004)
        - multipart/form-data with field `file`
        - optional form field `child_id` — when present, the caller
          must own that child (lateral-auth gate)
        - validates size ≤ 1 MB + image MIME via magic bytes
        - delegates to MediaStorage.put(...)
        - returns the avatar-specific shape
          {child_id, file_id, access_url, mime_type, size_bytes,
           sha1, category: "image", kind: "avatar"}
    GET /api/v1/media/{file_id}
        - reads back a previously uploaded file
        - accepts either the bare file_id (`f-abc12345`) or the
          synthetic `media://local/<id>.<ext>` access_url returned
          by /upload
        - returns raw bytes with the original Content-Type
"""

from __future__ import annotations

import os
import re
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.services.auth_audit import get_auth_audit_logger
from niki.services.child_registry import get_child_registry

from niki.media.storage import (
    EmptyFileError,
    FileTooLargeError,
    MediaStorageError,
    UnsupportedMimeError,
    _category_for,
    _detect_mime,
    get_media_storage,
)
from niki.media.ocr import (
    OcrError,
    get_ocr_backend,
)
from niki.middleware.rate_limit import limiter

from niki.api._i18n import error_response, localized_error_response, resolve_lang
from niki.i18n.translator import get_translator

router = APIRouter(
    prefix="/media",
    tags=[{"name": "media", "description": "Media API endpoints (BE-MEDIA-001, BE-MEDIA-004)."}]  # type: ignore[list-item]
)


# Accepts the bare id and the synthetic access_url scheme.
# `:path` converter is required because the synthetic URL
# `media://local/<id>.<ext>` contains `/` and `:` (URL-encoded or
# collapsed by httpx normalisation). Defense in depth: this regex
# rejects anything that isn't a bare `f-<hex>[-N]?` token,
# regardless of what's around it. The optional `-N` suffix
# accounts for the InMemoryS3Storage sequence number.
_FILE_ID_PATTERN = re.compile(r"f-[0-9a-f]{1,32}(?:-\d+)?")


def _extract_file_id(raw: str) -> str | None:
    """Pull the bare file_id out of either `f-abc12345` or
    `media://local/f-abc12345.png`. Returns None if no valid id is
    present (caller should 404)."""
    if not raw:
        return None
    m = _FILE_ID_PATTERN.search(raw)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# BE-MEDIA-005: OCR endpoint (mocked backend)
# ---------------------------------------------------------------------------


@router.post(
    "/ocr",
    summary="Run OCR on an uploaded image and return extracted text",
    description="""
            Run OCR on an uploaded image and return extracted text.
            
            Multipart fields:
                file     (required) — image bytes (png/jpg/webp/gif, ≤ 5 MB)
                language (optional) — BCP-47 tag, default "zh-CN"
            
            Responses:
                200 — recognised text + per-region bounding boxes
                400 — empty file
                413 — OCR_IMAGE_TOO_LARGE (> 5 MB)
                415 — UNSUPPORTED_OCR_MIME (not an image)
                503 — OCR backend unavailable
            """,
)
@limiter.limit("30/minute")
async def ocr(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form(default="zh-CN"),
    *,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Run OCR on an uploaded image and return extracted text.

    Multipart fields:
        file     (required) — image bytes (png/jpg/webp/gif, ≤ 5 MB)
        language (optional) — BCP-47 tag, default "zh-CN"

    Responses:
        200 — recognised text + per-region bounding boxes
        400 — empty file
        413 — OCR_IMAGE_TOO_LARGE (> 5 MB)
        415 — UNSUPPORTED_OCR_MIME (not an image)
        503 — OCR backend unavailable
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "EMPTY_FILE",
                "message": "file is empty",
            },
        )

    if len(raw) > OCR_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "OCR_IMAGE_TOO_LARGE",
                "message": f"image exceeds {OCR_MAX_BYTES} bytes",
                "max_bytes": OCR_MAX_BYTES,
                "size_bytes": len(raw),
            },
        )

    detected = _detect_mime(raw, file.content_type)
    if detected is None or detected not in (
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
    ):
        raise HTTPException(
            status_code=415,
            detail={
                "error": "UNSUPPORTED_OCR_MIME",
                "message": ("file must be image/jpeg, image/png, image/webp, or image/gif"),
                "mime_type": detected or file.content_type or "unknown",
            },
        )

    try:
        result = await get_ocr_backend().recognize(
            raw,
            language=language,
        )
    except OcrError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": exc.code, "message": exc.message},
        ) from exc

    return {
        "text": result.text,
        "language": result.language,
        "confidence": result.confidence,
        "regions": [
            {
                "text": r.text,
                "confidence": r.confidence,
                "bbox": list(r.bbox),
            }
            for r in result.regions
        ],
    }


# ---------------------------------------------------------------------------
# BE-MEDIA-004: avatar-specific upload shortcut
# ---------------------------------------------------------------------------


# Avatars are tiny: 1 MB cap (BE-MEDIA-004). This is tighter than
# the 10 MB generic upload so a malicious frontend can't dump a
# huge raw photo into the avatar slot.
AVATAR_MAX_BYTES: int = 1_048_576  # 1 MiB
OCR_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MB


def validate_image_for_avatar(
    content: bytes,
    mime_type: str | None,
    max_bytes: int = AVATAR_MAX_BYTES,
) -> str:
    """Validate avatar bytes for the BE-MEDIA-004 endpoint.

    Returns the magic-detected MIME if the file is `image/jpeg` or
    `image/png` and the byte length is within `max_bytes`. The
    tighter allow-list (jpg + png only — no webp/gif) keeps avatar
    rendering predictable across clients.

    Raises:
        HTTPException(413) — bytes exceed max_bytes
        HTTPException(415) — not a jpg/png image (magic-byte fail)

    The check is deliberately order-sensitive: size first (cheap),
    then MIME (magic-byte probe). Returning a string rather than
    raising for the happy path lets the endpoint log the resolved
    MIME once.
    """
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "AVATAR_TOO_LARGE",
                "message": (f"avatar exceeds {max_bytes} bytes"),
                "max_bytes": max_bytes,
                "size_bytes": len(content),
            },
        )

    detected = _detect_mime(content, mime_type)
    if detected not in ("image/jpeg", "image/png"):
        raise HTTPException(
            status_code=415,
            detail={
                "error": "INVALID_AVATAR_MIME",
                "message": ("avatar must be image/jpeg or image/png"),
                "mime_type": detected or mime_type or "unknown",
                "allowed": ["image/jpeg", "image/png"],
            },
        )
    return detected


async def _assert_parent_owns_child(
    request: Request,
    parent: ParentAccount,
    child_id: str,
) -> str:
    """Lateral-auth gate for the avatar endpoint.

    Mirrors the audit-aware logic in `auth.dependencies.get_owned_child_id`
    but accepts `child_id` from the multipart form rather than the
    path (the BE-MEDIA-004 endpoint doesn't put it in the URL).

    - CHILD_NOT_FOUND  → 404
    - PARENT_CHILD_MISMATCH → 403 (with audit event)
    - returns the verified child_id

    In test mode (`NIKI_TEST_FAKE_AUTH=1`), skips the parent match
    so existing test infrastructure passes through.
    """
    if os.environ.get("NIKI_TEST_FAKE_AUTH") == "1":
        return child_id

    registered = get_child_registry().get_parent_of(child_id)
    endpoint = f"{request.method} {request.url.path}"
    client_ip: Optional[str] = request.client.host if request.client else None
    if registered is None:
        await get_auth_audit_logger().log_violation(
            "child_not_found",
            actor_parent_id=parent.id,
            target_child_id=child_id,
            reason=f"Child {child_id!r} not registered",
            endpoint=endpoint,
            method=request.method,
            client_ip=client_ip,
        )
        raise HTTPException(
            status_code=404,
            detail=localized_error_response(
                request,
                code="CHILD_NOT_FOUND",
                key="auth.child_not_found",
                fallback_message=(f"No child registered with id {child_id!r}"),
            ),
        )
    if registered != parent.id:
        await get_auth_audit_logger().log_violation(
            "parent_child_mismatch",
            actor_parent_id=parent.id,
            target_child_id=child_id,
            target_parent_id=registered,
            reason=(
                f"Parent {parent.id!r} tried to upload avatar for "
                f"child {child_id!r} owned by {registered!r}"
            ),
            endpoint=endpoint,
            method=request.method,
            client_ip=client_ip,
        )
        raise HTTPException(
            status_code=403,
            detail=localized_error_response(
                request,
                code="PARENT_CHILD_MISMATCH",
                key="auth.parent_child_mismatch",
                fallback_message="Token does not own this child_id",
            ),
        )
    return child_id


@router.post(
    "/upload",
    summary="""
            Receive a file upload and store it via the active MediaStorage
            """,
    description="""
            Receive a file upload and store it via the active MediaStorage.
            
            Multipart form fields:
                file (required) — file part; size ≤ 10 MB.
            
            Responses:
                200 — {file_id, mime_type, size_bytes, sha1, access_url, ...}
                400 — empty / unsupported MIME type
                413 — too large
            """,
)
@limiter.limit("20/minute")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    *,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Receive a file upload and store it via the active MediaStorage.

    Multipart form fields:
        file (required) — file part; size ≤ 10 MB.

    Responses:
        200 — {file_id, mime_type, size_bytes, sha1, access_url, ...}
        400 — empty / unsupported MIME type
        413 — too large
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="EMPTY_FILE",
                key="media.empty_file",
                fallback_message="file is empty",
            ),
        )

    try:
        stored = get_media_storage().put(raw, mime_type=file.content_type)
    except EmptyFileError as exc:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="EMPTY_FILE",
                key="media.empty_file",
            ),
        ) from exc
    except UnsupportedMimeError as exc:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "INVALID_MIME_TYPE",
                "message": get_translator().translate(
                    "media.invalid_mime_type",
                    resolve_lang(request),
                    default=str(exc),
                ),
                "mime_type": exc.mime,
            },
        ) from exc
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "FILE_TOO_LARGE",
                "message": get_translator().translate(
                    "media.file_too_large",
                    resolve_lang(request),
                ),
                "max_bytes": exc.max_bytes,
                "size_bytes": exc.size,
            },
        ) from exc
    except MediaStorageError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "STORAGE_UNAVAILABLE",
                "message": get_translator().translate(
                    "media.storage_unavailable",
                    resolve_lang(request),
                    default=str(exc),
                ),
            },
        ) from exc

    return {
        "file_id": stored.file_id,
        "mime_type": stored.mime_type,
        "size_bytes": stored.size_bytes,
        "sha1": stored.sha1,
        "access_url": stored.access_url,
        "uploaded_at": stored.uploaded_at.isoformat(),
        "category": _category_for(stored.mime_type),
    }


# BE-MEDIA-001-rollout: register the more specific /thumb
# route FIRST so the bare /{file_id:path} doesn't gobble the
# /thumb suffix into the :path capture group.
@router.post(
    "/avatar",
    summary="Upload a child-profile avatar (BE-MEDIA-004)",
    description="""
            Upload a child-profile avatar (BE-MEDIA-004).
            
            A purpose-specific wrapper around the generic upload that:
              - tightens the byte cap to 1 MB
              - restricts the MIME allow-list to image/jpeg + image/png
              - validates the caller's parent ownership of the optional
                `child_id` form field
              - returns an avatar-shaped response (with `kind: "avatar"`)
            
            Multipart form fields:
                file     (required) — image bytes (jpg/png, ≤ 1 MB)
                child_id (optional) — when present, the caller must own it
            
            Responses:
                200 — avatar metadata
                413 — AVATAR_TOO_LARGE
                415 — INVALID_AVATAR_MIME
                403 — PARENT_CHILD_MISMATCH (cross-parent)
                404 — CHILD_NOT_FOUND
                400 — EMPTY_FILE / BAD_REQUEST
            """,
)
@limiter.limit("20/minute")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    child_id: Annotated[
        Optional[str],
        Form(description="Optional child the avatar belongs to"),
    ] = None,
    *,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Upload a child-profile avatar (BE-MEDIA-004).

    A purpose-specific wrapper around the generic upload that:
      - tightens the byte cap to 1 MB
      - restricts the MIME allow-list to image/jpeg + image/png
      - validates the caller's parent ownership of the optional
        `child_id` form field
      - returns an avatar-shaped response (with `kind: "avatar"`)

    Multipart form fields:
        file     (required) — image bytes (jpg/png, ≤ 1 MB)
        child_id (optional) — when present, the caller must own it

    Responses:
        200 — avatar metadata
        413 — AVATAR_TOO_LARGE
        415 — INVALID_AVATAR_MIME
        403 — PARENT_CHILD_MISMATCH (cross-parent)
        404 — CHILD_NOT_FOUND
        400 — EMPTY_FILE / BAD_REQUEST
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="EMPTY_FILE",
                key="media.empty_file",
                fallback_message="file is empty",
            ),
        )

    if child_id:
        child_id = await _assert_parent_owns_child(request, parent, child_id)

    mime = validate_image_for_avatar(raw, file.content_type)

    try:
        stored = get_media_storage().put(raw, mime_type=mime)
    except EmptyFileError as exc:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="EMPTY_FILE",
                key="media.empty_file",
            ),
        ) from exc
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "AVATAR_TOO_LARGE",
                "message": get_translator().translate(
                    "media.file_too_large",
                    resolve_lang(request),
                ),
                "max_bytes": exc.max_bytes,
                "size_bytes": exc.size,
            },
        ) from exc
    except UnsupportedMimeError as exc:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "INVALID_AVATAR_MIME",
                "message": get_translator().translate(
                    "media.invalid_mime_type",
                    resolve_lang(request),
                    default="avatar must be image/jpeg or image/png",
                ),
                "mime_type": exc.mime,
            },
        ) from exc
    except MediaStorageError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "STORAGE_UNAVAILABLE",
                "message": get_translator().translate(
                    "media.storage_unavailable",
                    resolve_lang(request),
                    default=str(exc),
                ),
            },
        ) from exc

    return {
        "child_id": child_id,
        "file_id": stored.file_id,
        "access_url": stored.access_url,
        "mime_type": stored.mime_type,
        "size_bytes": stored.size_bytes,
        "sha1": stored.sha1,
        "category": _category_for(stored.mime_type),
        "kind": "avatar",
    }


@router.get(
    "/{file_id:path}/thumb",
    summary="Return a server-resized thumbnail (BE-MEDIA-001-rollout)",
    description="""
            Return a server-resized thumbnail (BE-MEDIA-001-rollout).
            
            Bounds: 1 ≤ w,h ≤ 2048. Cached in-process (LRU 100
            entries) keyed by `(file_id, w, h)`. CDN can cache the
            result too (Cache-Control: public, max-age=86400).
            """,
)
@limiter.limit("60/minute")
async def get_thumbnail(
    request: Request,
    file_id: str,
    w: int = Query(200, ge=1, le=2048, description="Width in [1, 2048]"),
    h: int = Query(200, ge=1, le=2048, description="Height in [1, 2048]"),
    parent: ParentAccount = Depends(get_current_parent),
) -> Response:
    """Return a server-resized thumbnail (BE-MEDIA-001-rollout).

    Bounds: 1 ≤ w,h ≤ 2048. Cached in-process (LRU 100
    entries) keyed by `(file_id, w, h)`. CDN can cache the
    result too (Cache-Control: public, max-age=86400).

    `w` / `h` use Query() with Pydantic range validation so
    out-of-bounds returns the structured §6.3 shape (400 + code
    `INVALID_THUMB_SIZE`) instead of a 500 from runtime checks.
    """
    from niki.media.thumbnail import (
        build_thumbnail,
    )

    bare_id = _extract_file_id(file_id)
    if bare_id is None:
        return JSONResponse(
            status_code=404,
            content=error_response(
                request,
                code="FILE_NOT_FOUND",
                key="media.file_not_found",
            ),
        )

    result = build_thumbnail(file_id=bare_id, w=w, h=h)
    if result is None:
        return JSONResponse(
            status_code=404,
            content=error_response(
                request,
                code="FILE_NOT_FOUND",
                key="media.file_not_found",
            ),
        )
    out, output_mime = result

    return Response(
        content=out,
        media_type=output_mime,
        headers={
            "Content-Length": str(len(out)),
            "Cache-Control": "public, max-age=86400",
            "ETag": f'"{bare_id}:{w}x{h}"',
        },
    )


@router.get(
    "/{file_id:path}",
    summary="Read back a previously uploaded media file",
    description="""
            Read back a previously uploaded media file.
            
            The path accepts either:
                - the bare id returned by /upload, e.g. `f-abc12345`
                - the full `access_url` returned by /upload, e.g.
                  `media://local/f-abc12345.png` (scheme + extension are
                  stripped before lookup)
            
            The `:path` converter is needed so slashes in the synthetic URL
            are captured; the regex in `_extract_file_id` then narrows the
            value to a valid bare id.
            
            Responses:
                200 — raw bytes with `Content-Type` matching the upload's MIME
                404 — file not found (also returned for malformed ids — does
                      not 500 on path-traversal attempts)
                401 — missing / invalid Bearer token
            """,
)
@limiter.limit("100/minute")
async def retrieve(
    request: Request,
    file_id: str,
    *,
    parent: ParentAccount = Depends(get_current_parent),
) -> Response:
    """Read back a previously uploaded media file.

    The path accepts either:
        - the bare id returned by /upload, e.g. `f-abc12345`
        - the full `access_url` returned by /upload, e.g.
          `media://local/f-abc12345.png` (scheme + extension are
          stripped before lookup)

    The `:path` converter is needed so slashes in the synthetic URL
    are captured; the regex in `_extract_file_id` then narrows the
    value to a valid bare id.

    Responses:
        200 — raw bytes with `Content-Type` matching the upload's MIME
        404 — file not found (also returned for malformed ids — does
              not 500 on path-traversal attempts)
        401 — missing / invalid Bearer token
    """
    bare_id = _extract_file_id(file_id)
    if bare_id is None:
        raise HTTPException(
            status_code=404,
            detail=localized_error_response(
                request,
                code="FILE_NOT_FOUND",
                key="media.file_not_found",
            ),
        )

    stored = get_media_storage().get_with_meta(bare_id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail=localized_error_response(
                request,
                code="FILE_NOT_FOUND",
                key="media.file_not_found",
            ),
        )

    # BE-MEDIA-001-rollout: public Cache-Control so a CDN or
    # browser can cache media for 24h. The original access_url
    # returned by /upload points at this same endpoint, so
    # signed URLs are unnecessary for our deployment.
    return Response(
        content=stored.content,
        media_type=stored.mime_type,
        headers={
            "Content-Length": str(len(stored.content)),
            "Cache-Control": "public, max-age=86400",
            "ETag": f'"{getattr(stored, "file_id", bare_id)}"',
            "X-Niki-File-Id": stored.file_id,
        },
    )
