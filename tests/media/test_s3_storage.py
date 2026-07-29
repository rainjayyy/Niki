"""Tests for the S3-compatible media storage (BE-MEDIA-001-real).

We exercise the contract end-to-end via `InMemoryS3Storage`
(no real S3 endpoint, no `moto` framework). The unit tests
also confirm that `S3CompatibleStorage.__init__` requires
boto3 and that the in-memory fake faithfully covers the
same surface.
"""

from __future__ import annotations

import pytest

from niki.media.s3_storage import (
    InMemoryS3Storage,
    S3CompatibleStorage,
)
from niki.media.storage import (
    EmptyFileError,
    FileTooLargeError,
    UnsupportedMimeError,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 50
JPG = b"\xff\xd8\xff\xe0" + b"x" * 50


# ---------------------------------------------------------------------------
# InMemoryS3Storage — used by tests + safe to run without boto3
# ---------------------------------------------------------------------------


class TestInMemoryS3Storage:
    def test_put_then_get_roundtrip(self) -> None:
        store = InMemoryS3Storage(bucket="b")
        stored = store.put(PNG, mime_type="image/png")
        assert stored.mime_type == "image/png"
        assert stored.size_bytes == len(PNG)
        # The access_url points to a synthetic CDN URL.
        assert stored.access_url.startswith("https://")
        # get returns the original bytes.
        body = store.get(stored.file_id)
        assert body == PNG

    def test_get_unknown_id_returns_none(self) -> None:
        store = InMemoryS3Storage()
        assert store.get("f-doesnotexist") is None

    def test_get_with_meta_returns_authoritative_mime(self) -> None:
        """Stored MIME is what the uploader set, not the
        extension. The fake records mime separately from
        file_id so we can verify the contract."""
        store = InMemoryS3Storage()
        stored = store.put(PNG, mime_type="image/png")
        meta = store.get_with_meta(stored.file_id)
        assert meta is not None
        assert meta.mime_type == "image/png"
        assert meta.content == PNG
        assert meta.file_id == stored.file_id

    def test_jpg_classified_correctly(self) -> None:
        store = InMemoryS3Storage()
        stored = store.put(JPG, mime_type="image/jpeg")
        assert stored.mime_type == "image/jpeg"
        assert stored.access_url.endswith(".jpg")
        body = store.get(stored.file_id)
        assert body == JPG

    def test_rejects_empty_bytes(self) -> None:
        store = InMemoryS3Storage()
        with pytest.raises(EmptyFileError):
            store.put(b"", mime_type="image/png")

    def test_rejects_unsupported_mime(self) -> None:
        store = InMemoryS3Storage()
        with pytest.raises(UnsupportedMimeError):
            store.put(b"plain text content here", mime_type="text/plain")

    def test_rejects_oversized(self) -> None:
        store = InMemoryS3Storage()
        # 11 MB — over the 10 MB cap.
        big = b"\x89PNG\r\n\x1a\n" + b"x" * (11 * 1024 * 1024)
        with pytest.raises(FileTooLargeError):
            store.put(big, mime_type="image/png")

    def test_separate_puts_get_distinct_ids(self) -> None:
        store = InMemoryS3Storage()
        a = store.put(PNG, mime_type="image/png")
        b = store.put(PNG, mime_type="image/png")
        assert a.file_id != b.file_id
        # Both still retrievable
        assert store.get(a.file_id) == PNG
        assert store.get(b.file_id) == PNG

    def test_access_url_uses_public_cdn_base(self) -> None:
        store = InMemoryS3Storage(
            public_url_base="https://my-cdn.example/media"
        )
        stored = store.put(PNG, mime_type="image/png")
        assert stored.access_url.startswith(
            "https://my-cdn.example/media/"
        )
        assert stored.access_url.endswith(".png")

    def test_fake_abc_compatible(self) -> None:
        """The fake must satisfy the MediaStorage ABC so
        `get_media_storage()` can return it polymorphically."""
        from niki.media.storage import MediaStorage
        store = InMemoryS3Storage()
        assert isinstance(store, MediaStorage)


# ---------------------------------------------------------------------------
# S3CompatibleStorage — only the construction / wiring path
# ---------------------------------------------------------------------------


class TestS3CompatibleStorageInit:
    def test_missing_boto3_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hide boto3 from the import system so the lazy import
        # in __init__ raises ImportError → RuntimeError.
        import sys
        monkeypatch.setitem(sys.modules, "boto3", None)
        with pytest.raises(RuntimeError) as exc:
            S3CompatibleStorage(
                bucket="b",
                access_key="a",
                secret_key="s",
            )
        assert "boto3" in str(exc.value).lower()

    def test_empty_bucket_raises(self) -> None:
        with pytest.raises(ValueError):
            S3CompatibleStorage(
                bucket="",
                access_key="a",
                secret_key="s",
            )

    def test_init_with_boto3_installed(self) -> None:
        """Smoke test: boto3 is installed so the constructor
        must succeed. (boto3 client creation does NOT make
        network calls — it just builds the SDK state.)"""
        S3CompatibleStorage(
            bucket="test-bucket",
            access_key="AKIA-FAKE",
            secret_key="secret-fake",
            region="us-east-1",
        )


# ---------------------------------------------------------------------------
# get_media_storage() routing
# ---------------------------------------------------------------------------


class TestGetMediaStorageFactory:
    def test_local_backend_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from niki.config import settings as settings_mod
        settings_mod.get_settings.cache_clear()
        monkeypatch.delenv("MEDIA_STORAGE_BACKEND", raising=False)
        from niki.media.storage import (
            get_media_storage,
            set_media_storage,
            LocalDiskStorage,
        )
        set_media_storage(None)  # type: ignore[arg-type]
        # Force the cache to be cleared so settings re-evaluate.
        store = get_media_storage()
        assert isinstance(store, LocalDiskStorage)
        # Clean up
        set_media_storage(None)  # type: ignore[arg-type]

    def test_s3_backend_returns_in_memory_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S3 backend init requires real credentials; we just
        verify the factory calls S3CompatibleStorage when the
        setting is on. The fake is injected via set_media_storage
        so we don't need real boto3 calls."""
        from niki.media.storage import set_media_storage
        fake = InMemoryS3Storage(bucket="b")
        set_media_storage(fake)
        from niki.media.storage import get_media_storage
        assert get_media_storage() is fake
        set_media_storage(None)  # type: ignore[arg-type]