"""Server-side image thumbnails (BE-MEDIA-001-rollout).

Generates a resized JPEG/PNG on demand from the original
media bytes held by `MediaStorage`. Uses Pillow (PIL) for the
heavy lifting; the result is cached in an in-process LRU
keyed by `(file_id, w, h)` so repeat requests hit memory.

Worst-case memory budget = `LRU_MAX_ENTRIES` ×
`MAX_THUMB_BYTES` = 100 × ~200 KB ≈ 20 MB.
"""

from __future__ import annotations

import io
import threading
from collections import OrderedDict
from typing import Any

import structlog

from niki.media.storage import (
    MediaStorage,
    StoredContent,
    get_media_storage,
)

logger = structlog.get_logger(__name__)


# Allowed bounding box (1..2048). Anything outside is a 422.
MIN_DIM: int = 1
MAX_DIM: int = 2048

# In-process LRU cache cap.
LRU_MAX_ENTRIES: int = 100

# Output size cap. Pillow will refuse to allocate more than
# this; protects against `?w=99999` requests eating RAM.
MAX_THUMB_BYTES: int = 5 * 1024 * 1024

# Pillow's `thumbnail()` keeps aspect ratio and fits inside
# the box; we don't need custom math.
_OUTPUT_FORMAT: dict[str, str] = {
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}


class LRU:
    """Tiny thread-safe LRU keyed by string → bytes.

    We avoid `functools.lru_cache` because the value (bytes)
    is technically hashable but the cache size limit applies
    to entries, not bytes — we want a hard byte cap.
    """

    def __init__(self, max_entries: int = LRU_MAX_ENTRIES) -> None:
        self._max = max_entries
        self._d: "OrderedDict[str, bytes]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            if key not in self._d:
                return None
            # Move to end (most recently used)
            self._d.move_to_end(key)
            return self._d[key]

    def set(self, key: str, value: bytes) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self._max:
                self._d.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


# Module-level singleton — shared across requests. The thumbnail
# cache is global so requests hitting the same path don't redo
# work. Tests can call `set_thumbnail_cache(...)` to inject a
# fresh LRU per test.
_thumb_cache: LRU | None = None
_thumb_cache_lock = threading.Lock()


def get_thumbnail_cache() -> LRU:
    global _thumb_cache
    with _thumb_cache_lock:
        if _thumb_cache is None:
            _thumb_cache = LRU()
        return _thumb_cache


def set_thumbnail_cache(cache: LRU | None) -> None:
    """Override the module-level thumbnail cache (tests)."""
    global _thumb_cache
    with _thumb_cache_lock:
        _thumb_cache = cache


# ---------------------------------------------------------------------------
# Thumbnail builder
# ---------------------------------------------------------------------------


def _validate_dims(w: int, h: int) -> tuple[int, int]:
    if not (MIN_DIM <= w <= MAX_DIM) or not (MIN_DIM <= h <= MAX_DIM):
        raise ValueError(f"w and h must be in [{MIN_DIM}, {MAX_DIM}]; got w={w} h={h}")
    return w, h


def build_thumbnail(
    *,
    file_id: str,
    w: int,
    h: int,
    storage: MediaStorage | None = None,
) -> tuple[bytes, str] | None:
    """Return the resized bytes and the actual output mime type
    for the (file_id, w, h) tuple, or None if the source file
    is missing.

    The output mime matches the input format (PNG → PNG, etc.)
    so the endpoint can serve the right `Content-Type` header
    instead of lying.

    Caches the result in the LRU. Re-entrant: same args
    return the cached bytes.
    """
    w, h = _validate_dims(w, h)
    cache = get_thumbnail_cache()
    cache_key = f"{file_id}:{w}x{h}"
    cached = cache.get(cache_key)
    if cached is not None:
        # Cache stores (bytes, mime) tuple.
        return cached

    storage = storage or get_media_storage()
    stored: StoredContent | None = storage.get_with_meta(file_id)
    if stored is None:
        return None

    rendered = _render_thumbnail(stored.content, stored.mime_type, w, h)
    if rendered is not None:
        out_bytes, out_mime = rendered
        result = (out_bytes, out_mime)
        cache.set(cache_key, result)
        return result
    return None


def _render_thumbnail(content: bytes, mime_type: str, w: int, h: int) -> tuple[bytes, str] | None:
    """Run Pillow to resize. Returns (bytes, mime) or None."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        logger.error(
            "media.thumbnail.pillow_missing",
            hint="pip install pillow",
        )
        return None

    fmt = _OUTPUT_FORMAT.get(mime_type.lower())
    if not fmt:
        # Not a known image format. Return None so the caller
        # can fall back to the original.
        return None

    try:
        with Image.open(io.BytesIO(content)) as im:
            im.thumbnail((w, h))
            buf = io.BytesIO()
            save_kwargs: dict[str, Any] = {}
            if fmt == "JPEG":
                # JPEG needs RGB; preserve aspect of PNG alpha
                if im.mode in ("RGBA", "P"):
                    im = im.convert("RGB")
                save_kwargs["quality"] = 85
                save_kwargs["optimize"] = True
            im.save(buf, format=fmt, **save_kwargs)
            data = buf.getvalue()
            if len(data) > MAX_THUMB_BYTES:  # pragma: no cover
                logger.warning(
                    "media.thumbnail.oversized",
                    size=len(data),
                    cap=MAX_THUMB_BYTES,
                )
                return None
            # Output mime mirrors the input so the endpoint can
            # serve a correct Content-Type header.
            return data, mime_type
    except Exception as exc:  # pragma: no cover
        logger.error(
            "media.thumbnail.render_failed",
            error=str(exc),
            mime_type=mime_type,
        )
        return None
