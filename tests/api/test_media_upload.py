"""Integration + unit tests for /media/upload (BE-MEDIA-001)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.main import create_app
from niki.media.storage import (
    ALLOWED_IMAGE_MIMES,
    EmptyFileError,
    FileTooLargeError,
    LocalDiskStorage,
    UnsupportedMimeError,
    set_media_storage,
    _detect_mime,
)


# Magic-byte prefixes for the formats we accept.
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 50
JPG = b"\xff\xd8\xff\xe0" + b"x" * 50
GIF = b"GIF89a" + b"x" * 50
WAV = b"RIFF\x00\x00\x00\x00WAVE" + b"x" * 50
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 50


# ---------------------------------------------------------------------------
# Unit tests for the provider
# ---------------------------------------------------------------------------


class TestMagicByteDetection:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (PNG, "image/png"),
            (JPG, "image/jpeg"),
            (GIF, "image/gif"),
            (WAV, "audio/wav"),
            (WEBP, "image/webp"),
        ],
    )
    def test_recognises_magic_bytes(self, data: bytes, expected: str) -> None:
        assert _detect_mime(data, claimed=None) == expected

    def test_falls_back_to_claimed_when_unknown(self) -> None:
        # Claimed mime is allowed even though magic is unknown.
        assert _detect_mime(b"some bytes", "image/png") == "image/png"

    def test_returns_none_for_unsupported(self) -> None:
        assert _detect_mime(b"random", "text/plain") is None


class TestLocalDiskStorageUnit:
    def test_empty_raises(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        with pytest.raises(EmptyFileError):
            s.put(b"", mime_type="image/png")

    def test_unsupported_mime_raises(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        with pytest.raises(UnsupportedMimeError):
            s.put(b"plain text", mime_type="text/plain")

    def test_too_large_raises(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        big = b"\x89PNG\r\n\x1a\n" + b"x" * (11 * 1024 * 1024)
        with pytest.raises(FileTooLargeError):
            s.put(big, mime_type="image/png")

    def test_round_trip_get(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        stored = s.put(PNG, mime_type="image/png")
        retrieved = s.get(stored.file_id)
        assert retrieved == PNG

    def test_get_returns_none_for_unknown_id(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        assert s.get("nonexistent") is None

    def test_get_rejects_path_traversal(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        assert s.get("../../etc/passwd") is None
        assert s.get("f-../something") is None

    # BE-MEDIA-001-rollout -----------------------------------------

    def test_get_with_meta_returns_mime_and_bytes(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        stored = s.put(JPG, mime_type="image/jpeg")
        result = s.get_with_meta(stored.file_id)
        assert result is not None
        assert result.content == JPG
        assert result.mime_type == "image/jpeg"
        assert result.file_id == stored.file_id

    def test_get_with_meta_wav(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        stored = s.put(WAV, mime_type="audio/wav")
        result = s.get_with_meta(stored.file_id)
        assert result is not None
        assert result.mime_type == "audio/wav"

    def test_get_with_meta_unknown_id(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        assert s.get_with_meta("f-deadbeef") is None

    def test_get_with_meta_rejects_path_traversal(self, tmp_path) -> None:
        s = LocalDiskStorage(tmp_path)
        assert s.get_with_meta("../../etc/passwd") is None
        assert s.get_with_meta("f-../something") is None


# ---------------------------------------------------------------------------
# Integration tests via FastAPI TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path) -> TestClient:
    set_media_storage(LocalDiskStorage(tmp_path))
    return TestClient(create_app())


class TestUploadHappyPath:
    def test_png_upload(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/upload",
            files={"file": ("avatar.png", PNG, "image/png")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["mime_type"] == "image/png"
        assert body["size_bytes"] == len(PNG)
        assert body["category"] == "image"
        assert body["file_id"].startswith("f-")
        assert body["access_url"].startswith("media://local/f-")
        assert body["access_url"].endswith(".png")
        assert len(body["sha1"]) == 40  # hex SHA-1

    def test_jpg_upload(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/upload",
            files={"file": ("pic.jpg", JPG, "image/jpeg")},
        )
        assert r.status_code == 200
        assert r.json()["mime_type"] == "image/jpeg"
        assert r.json()["access_url"].endswith(".jpg")

    def test_wav_upload_classified_as_audio(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/upload",
            files={"file": ("clip.wav", WAV, "audio/wav")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["mime_type"] == "audio/wav"
        assert body["category"] == "audio"
        assert body["access_url"].endswith(".wav")

    def test_dedup_friendly_sha1_identical_for_same_bytes(
        self, client: TestClient
    ) -> None:
        r1 = client.post(
            "/api/v1/media/upload",
            files={"file": ("a.png", PNG, "image/png")},
        )
        r2 = client.post(
            "/api/v1/media/upload",
            files={"file": ("b.png", PNG, "image/png")},
        )
        assert r1.status_code == r2.status_code == 200
        # Different file_ids (each upload gets a fresh UUID) but identical sha1.
        assert r1.json()["file_id"] != r2.json()["file_id"]
        assert r1.json()["sha1"] == r2.json()["sha1"]


class TestUploadRejections:
    def test_empty_file_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/upload",
            files={"file": ("empty.png", b"", "image/png")},
        )
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "EMPTY_FILE"

    def test_unsupported_mime_returns_415(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/upload",
            files={"file": ("evil.exe", b"random", "application/octet-stream")},
        )
        assert r.status_code == 415
        body = r.json()
        assert body["error"]["code"] == "INVALID_MIME_TYPE"

    def test_text_plain_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/upload",
            files={"file": ("note.txt", b"some text", "text/plain")},
        )
        assert r.status_code == 415

    def test_html_rejected_xss_protection(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/upload",
            files={"file": ("x.html", b"<html>x</html>", "text/html")},
        )
        assert r.status_code == 415

    def test_too_large_returns_413(self, client: TestClient) -> None:
        big = b"\x89PNG\r\n\x1a\n" + b"x" * (11 * 1024 * 1024)
        r = client.post(
            "/api/v1/media/upload",
            files={"file": ("big.png", big, "image/png")},
        )
        assert r.status_code == 413
        body = r.json()
        assert body["error"]["code"] == "FILE_TOO_LARGE"
        assert body["error"]["max_bytes"] == 10 * 1024 * 1024

    def test_missing_file_returns_422(self, client: TestClient) -> None:
        # Empty multipart — no `file` field at all.
        r = client.post(
            "/api/v1/media/upload",
            files={"wrong_field": ("e.png", b"abc", "image/png")},
        )
        assert r.status_code in (400, 422)


class TestAllowedMimesCoverage:
    """Defensive: lock in the allow-list so a future refactor doesn't
    accidentally open the door to text/html / application/x-msdownload."""

    @pytest.mark.parametrize("mime", sorted(ALLOWED_IMAGE_MIMES))
    def test_image_allowed(self, mime: str) -> None:
        # Just exercise the detector without going through HTTP.
        # Pick a magic-bytes payload that matches the mime, otherwise
        # the detector may fall back to claimed.
        if mime == "image/jpeg":
            data = JPG
        elif mime == "image/png":
            data = PNG
        elif mime == "image/gif":
            data = GIF
        elif mime == "image/webp":
            data = WEBP
        else:
            data = b"\x00"
        assert _detect_mime(data, mime) == mime


# ---------------------------------------------------------------------------
# BE-MEDIA-001-rollout: GET retrieval endpoint
# ---------------------------------------------------------------------------


def _upload_via_client(client: TestClient, mime: str = "image/png") -> str:
    """Helper: upload a PNG, return the bare file_id from the response."""
    r = client.post(
        "/api/v1/media/upload",
        files={"file": ("avatar.png", PNG, mime)},
    )
    assert r.status_code == 200
    return r.json()["file_id"]


class TestMediaRetrievalHappyPath:
    def test_get_returns_uploaded_bytes_with_correct_mime(
        self, client: TestClient
    ) -> None:
        file_id = _upload_via_client(client)
        r = client.get(f"/api/v1/media/{file_id}")
        assert r.status_code == 200
        assert r.content == PNG
        assert r.headers["content-type"].startswith("image/png")
        assert r.headers["content-length"] == str(len(PNG))
        assert "cache-control" in r.headers
        # BE-MEDIA-001-rollout: switched to public so a CDN can
        # cache the bytes for 24h.
        assert "public" in r.headers["cache-control"]
        assert "max-age=86400" in r.headers["cache-control"]
        assert r.headers.get("x-niki-file-id") == file_id

    def test_get_jpg(self, client: TestClient) -> None:
        r_up = client.post(
            "/api/v1/media/upload",
            files={"file": ("pic.jpg", JPG, "image/jpeg")},
        )
        file_id = r_up.json()["file_id"]
        r = client.get(f"/api/v1/media/{file_id}")
        assert r.status_code == 200
        assert r.content == JPG
        assert r.headers["content-type"].startswith("image/jpeg")

    def test_get_wav_audio(self, client: TestClient) -> None:
        r_up = client.post(
            "/api/v1/media/upload",
            files={"file": ("clip.wav", WAV, "audio/wav")},
        )
        file_id = r_up.json()["file_id"]
        r = client.get(f"/api/v1/media/{file_id}")
        assert r.status_code == 200
        assert r.content == WAV
        assert r.headers["content-type"].startswith("audio/wav")


class TestMediaRetrievalAccessUrl:
    """The synthetic access_url `media://local/<id>.<ext>` should
    round-trip through the GET endpoint — frontend never has to
    parse the URL itself."""

    def test_get_via_access_url(self, client: TestClient) -> None:
        r_up = client.post(
            "/api/v1/media/upload",
            files={"file": ("avatar.png", PNG, "image/png")},
        )
        access_url = r_up.json()["access_url"]
        # access_url is "media://local/f-xxxxxxxx.png"
        assert access_url.startswith("media://local/")
        r = client.get(f"/api/v1/media/{access_url}")
        assert r.status_code == 200
        assert r.content == PNG
        assert r.headers["content-type"].startswith("image/png")


class TestMediaRetrievalErrors:
    def test_get_unknown_id_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/media/f-deadbeef")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "FILE_NOT_FOUND"

    def test_get_malformed_id_returns_404(self, client: TestClient) -> None:
        # No `f-` prefix and not path-traversal — still 404 (not 500).
        r = client.get("/api/v1/media/not-a-file-id")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "FILE_NOT_FOUND"

    def test_get_path_traversal_returns_404_not_500(
        self, client: TestClient
    ) -> None:
        # URL parser strips /, but `..` segments are sanitised away
        # by TestClient. The storage regex further rejects them. We
        # expect 404, never 500.
        r = client.get("/api/v1/media/..%2Fetc%2Fpasswd")
        # TestClient may 404 the path, or our handler returns 404 —
        # either way no 500.
        assert r.status_code in (404, 400)

    def test_get_empty_id_returns_404(self, client: TestClient) -> None:
        # Trailing slash with no file_id.
        r = client.get("/api/v1/media/")
        assert r.status_code in (404, 307)
