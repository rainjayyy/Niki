"""Media storage abstraction (BE-MEDIA-001).

The contract:
    - `put(content, mime_type) -> StoredFile` persists bytes and returns
      stable metadata (file_id, sha1, access_url).
    - `get(file_id) -> bytes | None` reads back the content (used by the
      retrieval endpoint added in a follow-up ticket).

Two implementations ship:
    - `LocalDiskStorage` — default; saves to `{settings.temp_upload_dir}`.
    - `MediaStorage`     — abstract base; production plug-in point for
                          OSS / COS / S3 (BE-MEDIA-001-real).

The default's `access_url` is a synthetic `media://local/<id>` scheme
that's understood by the demo frontend; production swap-out writes
HTTPS OSS URLs.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredFile:
    """Outcome of a successful put()."""

    file_id: str
    mime_type: str
    size_bytes: int
    sha1: str
    access_url: str
    uploaded_at: datetime


@dataclass(frozen=True)
class StoredContent:
    """Outcome of a successful get_with_meta()."""

    content: bytes
    mime_type: str
    file_id: str


class MediaStorageError(Exception):
    """Base class for storage failures."""


class EmptyFileError(MediaStorageError):
    pass


class UnsupportedMimeError(MediaStorageError):
    def __init__(self, mime: str) -> None:
        super().__init__(f"unsupported mime type: {mime!r}")
        self.mime = mime


class FileTooLargeError(MediaStorageError):
    def __init__(self, size: int, max_bytes: int) -> None:
        super().__init__(f"file {size} exceeds max {max_bytes}")
        self.size = size
        self.max_bytes = max_bytes


# ---------------------------------------------------------------------------
# MIME allow-list and magic-byte detection
# ---------------------------------------------------------------------------


ALLOWED_IMAGE_MIMES: Final = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)
ALLOWED_AUDIO_MIMES: Final = frozenset(
    {"audio/wav", "audio/x-wav", "audio/m4a", "audio/mp4", "audio/ogg", "audio/mpeg"}
)
ALLOWED_MIMES: Final = ALLOWED_IMAGE_MIMES | ALLOWED_AUDIO_MIMES
MAX_FILE_BYTES: Final = 10 * 1024 * 1024  # 10 MB


def _category_for(mime: str) -> str:
    if mime in ALLOWED_IMAGE_MIMES:
        return "image"
    if mime in ALLOWED_AUDIO_MIMES:
        return "audio"
    return "other"


def _detect_mime(data: bytes, claimed: str | None) -> str | None:
    """Best-effort magic-byte detection. Returns None if unsure."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    # WebP: RIFF + WEBP
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    # MP4 / M4A (ftyp at offset 4)
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "audio/mp4"
    # OGG (Opus / Vorbis)
    if data.startswith(b"OggS"):
        return "audio/ogg"
    # Fall back to claimed mime if it's an allowed one and magic is unknown.
    if claimed in ALLOWED_MIMES:
        return claimed
    return None


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------


class MediaStorage(ABC):
    """Contract for storing arbitrary media blobs."""

    @abstractmethod
    def put(self, content: bytes, *, mime_type: str | None = None) -> StoredFile:
        """Persist `content` and return its metadata.

        Args:
            content: Raw bytes (already validated for size and type).
            mime_type: The claimed Content-Type from the upload form;
                the implementation may re-derive via magic bytes.

        Raises:
            EmptyFileError: If `content` is empty.
            UnsupportedMimeError: If the resolved MIME is not allowed.
            FileTooLargeError: If `content` exceeds the impl's max.
        """

    @abstractmethod
    def get(self, file_id: str) -> bytes | None:
        """Read back the bytes for `file_id`, or None if not found."""

    def get_with_meta(self, file_id: str) -> StoredContent | None:
        """Read back the bytes plus MIME for `file_id`, or None.

        Default implementation derives `mime_type` from the file
        extension written by `put()`. Remote-storage providers
        (OSS / S3) should override this to query object metadata
        for an authoritative MIME rather than trusting extensions.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Local-disk implementation
# ---------------------------------------------------------------------------


class LocalDiskStorage(MediaStorage):
    """Default impl — saves uploads to a directory on local disk.

    The directory is created on first put(). `access_url` is the
    synthetic `media://local/<file_id>` scheme understood by the
    demo frontend.
    """

    def __init__(self, base_dir: str | os.PathLike[str]) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes, *, mime_type: str | None = None) -> StoredFile:
        if not content:
            raise EmptyFileError("upload is empty")
        if len(content) > MAX_FILE_BYTES:
            raise FileTooLargeError(len(content), MAX_FILE_BYTES)

        detected = _detect_mime(content, mime_type)
        if detected is None or detected not in ALLOWED_MIMES:
            raise UnsupportedMimeError(mime_type or "unknown")

        file_id = f"f-{uuid.uuid4().hex[:8]}"
        ext = _ext_for(detected)
        path = self._base / f"{file_id}{ext}"
        path.write_bytes(content)

        sha1 = hashlib.sha1(content).hexdigest()
        return StoredFile(
            file_id=file_id,
            mime_type=detected,
            size_bytes=len(content),
            sha1=sha1,
            access_url=f"media://local/{file_id}{ext}",
            uploaded_at=datetime.now(timezone.utc),
        )

    def get(self, file_id: str) -> bytes | None:
        # file_id must match our prefix and have no path-traversal chars
        if not re.fullmatch(r"f-[0-9a-f]{1,32}", file_id):
            return None
        for path in self._base.glob(f"{file_id}.*"):
            if path.is_file():
                return path.read_bytes()
        return None

    def get_with_meta(self, file_id: str) -> StoredContent | None:
        """Read back the bytes + MIME for `file_id`.

        Accepts the bare id (`f-abc12345`) — extension is discovered
        via glob, so callers don't need to know it.
        """
        if not re.fullmatch(r"f-[0-9a-f]{1,32}", file_id):
            return None
        for path in self._base.glob(f"{file_id}.*"):
            if path.is_file():
                return StoredContent(
                    content=path.read_bytes(),
                    mime_type=_mime_for_ext(path.suffix),
                    file_id=file_id,
                )
        return None


_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
}


# Reverse map for retrieval (BE-MEDIA-001-rollout). Multiple MIME
# types can share an extension (audio/wav + audio/x-wav); the
# canonical one for GET responses is listed here.
_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
}


def _ext_for(mime: str) -> str:
    return _EXT_BY_MIME.get(mime, ".bin")


def _mime_for_ext(ext: str) -> str:
    return _MIME_BY_EXT.get(ext.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_storage: MediaStorage | None = None


def get_media_storage() -> MediaStorage:
    """Return the process-wide media storage.

    BE-MEDIA-001-real: the active backend is driven by
    `settings.media_storage_backend`:
        "local" → LocalDiskStorage (single-process, dev / PoC)
        "s3"    → S3CompatibleStorage (production, boto3)

    Tests should call `set_media_storage(...)` to inject a fake
    (e.g. `InMemoryS3Storage`) so the S3 path can be exercised
    without a real endpoint.
    """
    global _storage
    if _storage is None:
        from niki.config.settings import get_settings
        settings = get_settings()
        if settings.media_storage_backend == "s3":
            # Lazy import — keeps boto3 optional for local dev.
            from niki.media.s3_storage import S3CompatibleStorage
            _storage = S3CompatibleStorage(
                endpoint_url=settings.s3_endpoint_url,
                region=settings.s3_region,
                bucket=settings.s3_bucket_name,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                presign_ttl_seconds=settings.s3_presign_ttl_seconds,
                addressing_style=settings.s3_addressing_style,
                multipart_threshold_bytes=(
                    settings.s3_multipart_threshold_bytes
                ),
            )
        else:
            base = os.environ.get(
                "NIKI_UPLOAD_DIR",
                str(Path.home() / ".niki" / "uploads"),
            )
            _storage = LocalDiskStorage(base)
    return _storage


def set_media_storage(storage: MediaStorage) -> None:
    """Override the process-wide media storage (for tests / OSS swap)."""
    global _storage
    _storage = storage
