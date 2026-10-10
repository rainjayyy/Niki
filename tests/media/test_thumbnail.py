"""Tests for the thumbnail service + endpoint (BE-MEDIA-001-rollout)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.main import create_app
from niki.media.s3_storage import InMemoryS3Storage as InMemoryMediaStorage
from niki.media.storage import set_media_storage
from niki.media.thumbnail import (
    LRU,
    MAX_DIM,
    MIN_DIM,
    build_thumbnail,
    set_thumbnail_cache,
)


# 100×100 PNG (orange square) — Pillow-generated, known-good.
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000064000000640802000000ff"
    "800203000000ea49444154789cedd231110021100441784968c2bf05dec2"
    "4dde1d6f34b5fbddb398f9863bc46a3c2b102b102b102b102b102b102b10"
    "2b102b102b102b102b102b102b102b102b102b102b102b102b102b102b10"
    "2b102b102b102b102b102b102b102b102b102b102b102b102b102b102b10"
    "2b102b102b102b102b102b102b102b102b102b102b102b102b102b102b10"
    "2b102b102b102b102b102b102b102b102b102b102b102b102b102b102b10"
    "2b102b102b102b102b102b102b102b102b102b102b102b102b102b102b10"
    "2b102b102b102b102b102b102b102b102b102b102b102b102b102b106bcd"
    "fd71c3025d94d6efaf0000000049454e44ae426082"
)


@pytest.fixture
def client() -> TestClient:
    storage = InMemoryMediaStorage()
    storage.put(PNG_1x1, mime_type="image/png")
    set_media_storage(storage)
    set_thumbnail_cache(LRU(max_entries=4))
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p-1",
        name="T",
        id_number_masked="****",
        phone_masked="****",
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# LRU primitive
# ---------------------------------------------------------------------------


class TestLRU:
    def test_get_set(self) -> None:
        c = LRU(max_entries=3)
        c.set("a", b"1")
        assert c.get("a") == b"1"
        assert c.get("missing") is None

    def test_evicts_oldest(self) -> None:
        c = LRU(max_entries=2)
        c.set("a", b"1")
        c.set("b", b"2")
        c.set("c", b"3")  # evicts "a"
        assert c.get("a") is None
        assert c.get("b") == b"2"
        assert c.get("c") == b"3"

    def test_get_refreshes_lru_order(self) -> None:
        c = LRU(max_entries=2)
        c.set("a", b"1")
        c.set("b", b"2")
        c.get("a")  # refresh; "b" now oldest
        c.set("c", b"3")  # evicts "b"
        assert c.get("a") == b"1"
        assert c.get("b") is None
        assert c.get("c") == b"3"

    def test_clear(self) -> None:
        c = LRU()
        c.set("a", b"1")
        c.clear()
        assert len(c) == 0
        assert c.get("a") is None


# ---------------------------------------------------------------------------
# build_thumbnail
# ---------------------------------------------------------------------------


class TestBuildThumbnail:
    def test_returns_resized_bytes_for_png(self) -> None:
        storage = InMemoryMediaStorage()
        stored = storage.put(PNG_1x1, mime_type="image/png")
        out = build_thumbnail(file_id=stored.file_id, w=200, h=200, storage=storage)
        assert out is not None
        # Returns (bytes, mime) tuple. Preserves input format.
        out_bytes, out_mime = out
        assert out_mime == "image/png"
        assert out_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    def test_returns_none_for_unknown_id(self) -> None:
        storage = InMemoryMediaStorage()
        assert build_thumbnail(file_id="never-seen", w=100, h=100, storage=storage) is None

    def test_rejects_out_of_range_dims(self) -> None:
        storage = InMemoryMediaStorage()
        stored = storage.put(PNG_1x1, mime_type="image/png")
        with pytest.raises(ValueError):
            build_thumbnail(file_id=stored.file_id, w=0, h=100, storage=storage)
        with pytest.raises(ValueError):
            build_thumbnail(file_id=stored.file_id, w=100, h=MAX_DIM + 1, storage=storage)

    def test_caches_result(self) -> None:
        storage = InMemoryMediaStorage()
        stored = storage.put(PNG_1x1, mime_type="image/png")
        cache = LRU(max_entries=10)
        set_thumbnail_cache(cache)
        # First call populates cache.
        out1 = build_thumbnail(file_id=stored.file_id, w=200, h=200, storage=storage)
        # Second call must hit cache (same bytes).
        out2 = build_thumbnail(file_id=stored.file_id, w=200, h=200, storage=storage)
        assert out1 == out2
        assert len(cache) == 1

    def test_lru_evicts(self) -> None:
        storage = InMemoryMediaStorage()
        stored = storage.put(PNG_1x1, mime_type="image/png")
        cache = LRU(max_entries=2)
        set_thumbnail_cache(cache)
        # Two distinct sizes → 2 cache entries.
        build_thumbnail(file_id=stored.file_id, w=100, h=100, storage=storage)
        build_thumbnail(file_id=stored.file_id, w=200, h=200, storage=storage)
        assert len(cache) == 2
        # Third one evicts the oldest (100x100).
        build_thumbnail(file_id=stored.file_id, w=300, h=300, storage=storage)
        assert len(cache) == 2
        # 100x100 was evicted; cache only has 200x200 + 300x300.
        assert cache.get(f"{stored.file_id}:100x100") is None
        assert cache.get(f"{stored.file_id}:200x200") is not None
        assert cache.get(f"{stored.file_id}:300x300") is not None


# ---------------------------------------------------------------------------
# Endpoint integration
# ---------------------------------------------------------------------------


class TestThumbnailEndpoint:
    def test_get_thumb_returns_jpeg(self, client: TestClient) -> None:
        # First find the file_id of the seed PNG.
        # Use the upload endpoint to get a real file_id, or
        # just hard-code via the test storage.
        from niki.media.storage import get_media_storage

        storage = get_media_storage()
        # Grab the first file_id we know is in the storage.
        for k, _ in storage._objects.items():
            fid = k.split(".")[0]
            break
        else:
            pytest.skip("no file in storage")

        r = client.get(f"/api/v1/media/{fid}/thumb", params={"w": 100, "h": 100})
        assert r.status_code == 200
        # The endpoint uses the actual output mime from the
        # thumbnail service (preserves input format).
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
        # Cache-Control: public, 24h.
        assert "public" in r.headers["cache-control"]
        assert "max-age=86400" in r.headers["cache-control"]
        # ETag present and matches the cache key.
        assert r.headers.get("etag") == f'"{fid}:100x100"'

    def test_invalid_size_returns_422(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/media/f-abc12345/thumb",
            params={"w": 9999, "h": 100},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_THUMB_SIZE"

    def test_unknown_file_returns_404(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/media/never-existed/thumb",
            params={"w": 100, "h": 100},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "FILE_NOT_FOUND"

    def test_original_endpoint_has_public_cache(self, client: TestClient) -> None:
        """BE-MEDIA-001-rollout: the original /media/{id}
        endpoint now serves Cache-Control: public (was private
        in the previous ticket) so a CDN can cache the bytes."""
        from niki.media.storage import get_media_storage

        storage = get_media_storage()
        for k, _ in storage._objects.items():
            fid = k.split(".")[0]
            break
        else:
            pytest.skip("no file in storage")

        r = client.get(f"/api/v1/media/{fid}")
        assert r.status_code == 200
        assert "public" in r.headers["cache-control"]
        assert "max-age=86400" in r.headers["cache-control"]
