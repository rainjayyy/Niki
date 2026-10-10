"""Integration + unit tests for /media/avatar (BE-MEDIA-004).

Covers:
    - happy path (1x1 PNG → 200 with avatar shape)
    - too big (>1 MB) → 413
    - wrong mime (text) → 415
    - cross-parent child_id → 403
    - the returned access_url is the synthetic media://local/...
    - empty file → 400
    - missing child_id is allowed (the v1 path)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.api.media import (
    AVATAR_MAX_BYTES,
    validate_image_for_avatar,
)
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.main import create_app
from niki.media.storage import (
    LocalDiskStorage,
    set_media_storage,
)
from niki.services.child_registry import (
    InMemoryChildRegistry,
    set_child_registry,
)


# Magic-byte payloads
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 50
JPG = b"\xff\xd8\xff\xe0" + b"x" * 50
TXT = b"hello, this is a text file, not an image" * 4


# ---------------------------------------------------------------------------
# Unit tests for validate_image_for_avatar()
# ---------------------------------------------------------------------------


class TestValidateImageForAvatar:
    def test_png_passes(self) -> None:
        mime = validate_image_for_avatar(PNG, "image/png")
        assert mime == "image/png"

    def test_jpg_passes(self) -> None:
        mime = validate_image_for_avatar(JPG, "image/jpeg")
        assert mime == "image/jpeg"

    def test_text_raises_415(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            validate_image_for_avatar(TXT, "text/plain")
        assert exc_info.value.status_code == 415
        assert exc_info.value.detail["error"] == "INVALID_AVATAR_MIME"

    def test_too_large_raises_413(self) -> None:
        from fastapi import HTTPException

        # Exactly max+1 bytes
        big = PNG + b"x" * (AVATAR_MAX_BYTES - len(PNG) + 1)
        with pytest.raises(HTTPException) as exc_info:
            validate_image_for_avatar(big, "image/png")
        assert exc_info.value.status_code == 413
        assert exc_info.value.detail["error"] == "AVATAR_TOO_LARGE"
        assert exc_info.value.detail["max_bytes"] == AVATAR_MAX_BYTES

    def test_exactly_max_size_passes(self) -> None:
        # size == max_bytes (not > max_bytes)
        payload = PNG + b"x" * (AVATAR_MAX_BYTES - len(PNG))
        assert len(payload) == AVATAR_MAX_BYTES
        mime = validate_image_for_avatar(payload, "image/png")
        assert mime == "image/png"

    def test_webp_rejected(self) -> None:
        from fastapi import HTTPException

        # WebP magic bytes — even though BE-MEDIA-001 generic
        # upload accepts it, BE-MEDIA-004 rejects it for avatars.
        webp = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 50
        with pytest.raises(HTTPException) as exc_info:
            validate_image_for_avatar(webp, "image/webp")
        assert exc_info.value.status_code == 415


# ---------------------------------------------------------------------------
# Integration tests — happy path + size / mime rejections
# (uses the conftest fake-auth override; cross-parent is a separate fixture)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path) -> TestClient:
    set_media_storage(LocalDiskStorage(tmp_path))
    return TestClient(create_app())


class TestAvatarHappyPath:
    def test_png_returns_avatar_shape(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/avatar",
            files={"file": ("avatar.png", PNG, "image/png")},
        )
        assert r.status_code == 200
        body = r.json()
        # Avatar-specific shape
        assert body["kind"] == "avatar"
        assert body["category"] == "image"
        assert body["mime_type"] == "image/png"
        assert body["size_bytes"] == len(PNG)
        assert body["file_id"].startswith("f-")
        # access_url is the synthetic media://local/<id>.<ext>
        assert body["access_url"].startswith("media://local/f-")
        assert body["access_url"].endswith(".png")
        assert len(body["sha1"]) == 40  # hex SHA-1

    def test_jpg_returns_avatar_shape(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/avatar",
            files={"file": ("avatar.jpg", JPG, "image/jpeg")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "avatar"
        assert body["mime_type"] == "image/jpeg"
        assert body["access_url"].endswith(".jpg")

    def test_no_child_id_allowed(self, client: TestClient) -> None:
        """v1 path: caller uploads avatar without binding to a child yet."""
        r = client.post(
            "/api/v1/media/avatar",
            files={"file": ("avatar.png", PNG, "image/png")},
        )
        assert r.status_code == 200
        # child_id is null in the response (caller hasn't bound it).
        assert r.json()["child_id"] is None


class TestAvatarRejections:
    def test_empty_file_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/avatar",
            files={"file": ("empty.png", b"", "image/png")},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_FILE"

    def test_too_large_returns_413(self, client: TestClient) -> None:
        # 2 MB payload — well over the 1 MB avatar cap.
        big = PNG + b"x" * (2 * 1024 * 1024)
        r = client.post(
            "/api/v1/media/avatar",
            files={"file": ("big.png", big, "image/png")},
        )
        assert r.status_code == 413
        body = r.json()
        assert body["error"]["code"] == "AVATAR_TOO_LARGE"
        assert body["error"]["max_bytes"] == AVATAR_MAX_BYTES
        assert body["error"]["size_bytes"] == len(big)

    def test_text_returns_415(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/avatar",
            files={"file": ("note.txt", TXT, "text/plain")},
        )
        assert r.status_code == 415
        body = r.json()
        assert body["error"]["code"] == "INVALID_AVATAR_MIME"
        # Only jpg/png are allowed for avatars
        assert "image/jpeg" in body["error"]["allowed"]
        assert "image/png" in body["error"]["allowed"]

    def test_webp_rejected_for_avatar(self, client: TestClient) -> None:
        # WebP is allowed by the generic /upload but NOT by /avatar.
        webp = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 50
        r = client.post(
            "/api/v1/media/avatar",
            files={"file": ("avatar.webp", webp, "image/webp")},
        )
        assert r.status_code == 415
        assert r.json()["error"]["code"] == "INVALID_AVATAR_MIME"

    def test_missing_file_field_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/avatar",
            files={"wrong_field": ("e.png", b"abc", "image/png")},
        )
        # FastAPI returns 422 when the required `file` form field
        # is missing.
        assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Cross-parent lateral-auth — requires unsetting the conftest fake-auth
# override so the real ownership check runs.
# ---------------------------------------------------------------------------


@pytest.fixture
def authed_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    """TestClient where the conftest fake-auth override is disabled,
    `get_current_parent` returns a deterministic parent id, and the
    child registry has one child owned by another parent. Used to
    verify lateral-auth on the /avatar endpoint."""
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    set_media_storage(LocalDiskStorage(tmp_path))

    registry = InMemoryChildRegistry()
    # Victim child — owned by a different parent.
    registry.register("c_victim", "p_victim")
    # Own child — for the happy-path ownership case.
    registry.register("c_own", "p_actor")
    set_child_registry(registry)

    app = create_app()

    def fake():
        return ParentAccount(
            id="p_actor",
            name="Actor",
            id_number_masked="****",
            phone_masked="****",
        )

    app.dependency_overrides[get_current_parent] = fake
    return TestClient(app)


class TestAvatarLateralAuth:
    def test_cross_parent_child_id_returns_403(self, authed_client: TestClient) -> None:
        """Caller p_actor tries to upload an avatar for c_victim
        (owned by p_victim). Must 403."""
        r = authed_client.post(
            "/api/v1/media/avatar",
            files={"file": ("avatar.png", PNG, "image/png")},
            data={"child_id": "c_victim"},
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PARENT_CHILD_MISMATCH"

    def test_unknown_child_id_returns_404(self, authed_client: TestClient) -> None:
        r = authed_client.post(
            "/api/v1/media/avatar",
            files={"file": ("avatar.png", PNG, "image/png")},
            data={"child_id": "c_does_not_exist"},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "CHILD_NOT_FOUND"

    def test_owned_child_id_succeeds(self, authed_client: TestClient) -> None:
        """Caller p_actor uploading for c_own (which they own)."""
        r = authed_client.post(
            "/api/v1/media/avatar",
            files={"file": ("avatar.png", PNG, "image/png")},
            data={"child_id": "c_own"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "avatar"
        assert body["child_id"] == "c_own"
        assert body["access_url"].startswith("media://local/f-")


# ---------------------------------------------------------------------------
# Storage round-trip — the returned access_url must actually resolve
# ---------------------------------------------------------------------------


class TestAvatarAccessUrlRoundTrip:
    def test_access_url_resolves_through_retrieval_endpoint(self, client: TestClient) -> None:
        """The access_url returned by /avatar must round-trip through
        the same GET /media/{file_id} endpoint as a generic upload."""
        r_up = client.post(
            "/api/v1/media/avatar",
            files={"file": ("avatar.png", PNG, "image/png")},
        )
        assert r_up.status_code == 200
        access_url = r_up.json()["access_url"]

        r_get = client.get(f"/api/v1/media/{access_url}")
        assert r_get.status_code == 200
        assert r_get.content == PNG
        assert r_get.headers["content-type"].startswith("image/png")
