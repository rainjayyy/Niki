"""Media API endpoints (BE-MEDIA-001).

Currently exposes:
    POST /api/v1/media/upload
        - multipart/form-data with field `file`
        - validates MIME via magic bytes
        - delegates to MediaStorage.put(...)
        - returns {file_id, mime_type, size_bytes, sha1, access_url,
                   uploaded_at, category}
    GET /api/v1/media/{file_id}
        - reads back a previously uploaded file
        - accepts either the bare file_id (`f-abc12345`) or the
          synthetic `media://local/<id>.<ext>` access_url returned
          by /upload
        - returns raw bytes with the original Content-Type
"""

from __future__ import annotations

import base64
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from typing import Annotated

from fastapi import Depends

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent

from niki.media.storage import (
    EmptyFileError,
    FileTooLargeError,
    MediaStorageError,
    UnsupportedMimeError,
    get_media_storage,
    _category_for,
)
from niki.middleware.rate_limit import limiter

router = APIRouter(prefix="/media", tags=["media"])


# Accepts the bare id and the synthetic access_url scheme.
# `:path` converter is required because the synthetic URL
# `media://local/<id>.<ext>` contains `/` and `:` (URL-encoded or
# collapsed by httpx normalisation). Defense in depth: this regex
# rejects anything that isn't a bare `f-<hex>` token, regardless of
# what's around it.
_FILE_ID_PATTERN = re.compile(r"f-[0-9a-f]{1,32}")


def _extract_file_id(raw: str) -> str | None:
    """Pull the bare file_id out of either `f-abc12345` or
    `media://local/f-abc12345.png`. Returns None if no valid id is
    present (caller should 404)."""
    if not raw:
        return None
    m = _FILE_ID_PATTERN.search(raw)
    return m.group(0) if m else None


@router.post("/upload")
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
            detail={"error": "EMPTY_FILE", "message": "file is empty"},
        )

    try:
        stored = get_media_storage().put(raw, mime_type=file.content_type)
    except EmptyFileError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "EMPTY_FILE"}
        ) from exc
    except UnsupportedMimeError as exc:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "INVALID_MIME_TYPE",
                "message": str(exc),
                "mime_type": exc.mime,
            },
        ) from exc
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "FILE_TOO_LARGE",
                "max_bytes": exc.max_bytes,
                "size_bytes": exc.size,
            },
        ) from exc
    except MediaStorageError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "STORAGE_UNAVAILABLE",
                "message": str(exc),
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


@router.get("/{file_id:path}")
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
            detail={"error": "FILE_NOT_FOUND"},
        )

    stored = get_media_storage().get_with_meta(bare_id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "FILE_NOT_FOUND"},
        )

    return Response(
        content=stored.content,
        media_type=stored.mime_type,
        headers={
            "Content-Length": str(len(stored.content)),
            "Cache-Control": "private, max-age=86400",
            "X-Niki-File-Id": stored.file_id,
        },
    )
