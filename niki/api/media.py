"""Media API endpoints (BE-MEDIA-001).

Currently exposes:
    POST /api/v1/media/upload
        - multipart/form-data with field `file`
        - validates MIME via magic bytes
        - delegates to MediaStorage.put(...)
        - returns {file_id, mime_type, size_bytes, sha1, access_url,
                   uploaded_at, category}
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

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
