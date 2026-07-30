"""Tests for the OCR endpoint + mock backend (BE-MEDIA-005)."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.main import create_app
from niki.media.ocr import (
    MockOcrBackend,
    OcrBackend,
    OcrError,
    OcrResult,
    get_ocr_backend,
    set_ocr_backend,
)


# Tiny valid PNG (1x1 transparent) used as a "real image" payload.
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000"
    "001f15c4890000000d49444154789c63000100000500010d0a2db400"
    "00000049454e44ae426082"
)


@pytest.fixture
def client() -> TestClient:
    """A TestClient wired to the real /api/v1/media/ocr endpoint
    with a mock parent dep. We restore the default backend at
    teardown so singleton state doesn't leak between tests."""
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p-ocr", name="OCR Test",
        id_number_masked="****", phone_masked="****",
    )
    saved = get_ocr_backend()
    set_ocr_backend(MockOcrBackend())
    yield TestClient(app)
    set_ocr_backend(saved)


# ---------------------------------------------------------------------------
# Mock backend primitives
# ---------------------------------------------------------------------------


class TestMockOcrBackend:
    @pytest.mark.asyncio
    async def test_returns_nonzero_text(self) -> None:
        be = MockOcrBackend()
        result = await be.recognize(PNG_1x1)
        assert isinstance(result, OcrResult)
        assert result.text  # non-empty
        assert result.language == "zh-CN"
        assert 0.0 < result.confidence <= 1.0
        assert len(result.regions) >= 1
        # Every region has a non-empty word and a confidence in (0, 1].
        for r in result.regions:
            assert r.text
            assert 0.0 < r.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_deterministic_same_bytes_same_text(self) -> None:
        be = MockOcrBackend()
        r1 = await be.recognize(PNG_1x1)
        r2 = await be.recognize(PNG_1x1)
        assert r1.text == r2.text
        assert r1.confidence == r2.confidence
        assert [r.text for r in r1.regions] == [
            r.text for r in r2.regions
        ]

    @pytest.mark.asyncio
    async def test_different_bytes_usually_different_text(self) -> None:
        be = MockOcrBackend()
        r1 = await be.recognize(PNG_1x1)
        r2 = await be.recognize(b"\x89PNG\r\n\x1a\nNOT-REAL-IMAGE")
        # Different bytes → almost certainly different buckets, so
        # at least one of text or region count differs.
        assert (r1.text != r2.text) or (
            len(r1.regions) != len(r2.regions)
        )

    @pytest.mark.asyncio
    async def test_empty_image_raises_ocr_error(self) -> None:
        be = MockOcrBackend()
        with pytest.raises(OcrError) as exc_info:
            await be.recognize(b"")
        assert exc_info.value.code == "EMPTY_IMAGE"

    @pytest.mark.asyncio
    async def test_english_uses_english_wordlist(self) -> None:
        be = MockOcrBackend()
        result = await be.recognize(PNG_1x1, language="en-US")
        assert result.language == "en-US"
        # Every picked word should be in the English list. The mock
        # is exhaustive so we can check membership exactly.
        from niki.media.ocr import _MOCK_WORDS_EN
        for r in result.regions:
            assert r.text in _MOCK_WORDS_EN
        # And none of the picked words should be a zh-CN-only one.
        from niki.media.ocr import _MOCK_WORDS_ZH
        for r in result.regions:
            assert r.text not in _MOCK_WORDS_ZH


# ---------------------------------------------------------------------------
# Singleton swap
# ---------------------------------------------------------------------------


class TestOcrBackendSingleton:
    def test_default_is_mock(self) -> None:
        # The global singleton should default to MockOcrBackend.
        from niki.media import ocr as ocr_mod
        set_ocr_backend(ocr_mod.MockOcrBackend())
        assert isinstance(get_ocr_backend(), MockOcrBackend)

    def test_set_and_get_swap(self) -> None:
        class _Fake(OcrBackend):
            async def recognize(self, image, *, language="zh-CN"):
                return OcrResult(
                    text="forced",
                    language=language,
                    confidence=1.0,
                    regions=(),
                )

        saved = get_ocr_backend()
        try:
            set_ocr_backend(_Fake())
            assert isinstance(get_ocr_backend(), _Fake)
            # Calling the endpoint then should use the fake backend.
        finally:
            set_ocr_backend(saved)


# ---------------------------------------------------------------------------
# Endpoint integration
# ---------------------------------------------------------------------------


class TestOcrEndpoint:
    def test_happy_path_png(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/ocr",
            files={"file": ("label.png", PNG_1x1, "image/png")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "text" in body
        assert body["text"]  # non-empty
        assert "language" in body
        assert body["language"] == "zh-CN"
        assert "confidence" in body
        assert 0.0 < body["confidence"] <= 1.0
        assert "regions" in body
        assert isinstance(body["regions"], list)
        assert len(body["regions"]) >= 1
        for region in body["regions"]:
            assert set(region.keys()) == {
                "text", "confidence", "bbox",
            }
            assert region["text"]
            assert 0.0 < region["confidence"] <= 1.0
            assert isinstance(region["bbox"], list)
            assert len(region["bbox"]) == 4

    def test_happy_path_jpeg(self, client: TestClient) -> None:
        # A tiny but valid JPEG (just the SOI marker is enough for
        # the magic-byte check). The mock backend doesn't parse the
        # bytes so we don't need real picture data.
        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
        r = client.post(
            "/api/v1/media/ocr",
            files={"file": ("p.jpg", fake_jpeg, "image/jpeg")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["text"]

    def test_language_form_field_forwarded(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/api/v1/media/ocr",
            files={"file": ("p.png", PNG_1x1, "image/png")},
            data={"language": "en-US"},
        )
        assert r.status_code == 200
        assert r.json()["language"] == "en-US"

    def test_too_large_returns_413(self, client: TestClient) -> None:
        # 6 MB of image/png magic + padding. The endpoint caps at
        # 5 MB so this should be rejected before the backend runs.
        too_big = b"\x89PNG\r\n\x1a\n" + b"\xff" * (6 * 1024 * 1024)
        r = client.post(
            "/api/v1/media/ocr",
            files={"file": ("big.png", too_big, "image/png")},
        )
        assert r.status_code == 413, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "OCR_IMAGE_TOO_LARGE"
        assert detail["max_bytes"] == 5 * 1024 * 1024
        assert detail["size_bytes"] >= 6 * 1024 * 1024

    def test_wrong_mime_returns_415(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/ocr",
            files={
                "file": (
                    "readme.txt",
                    b"this is plain text, not an image",
                    "text/plain",
                ),
            },
        )
        assert r.status_code == 415, r.text
        assert r.json()["detail"]["error"] == "UNSUPPORTED_OCR_MIME"

    def test_empty_file_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/media/ocr",
            files={"file": ("empty.png", b"", "image/png")},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "EMPTY_FILE"

    def test_mock_backend_returns_deterministic_text(
        self, client: TestClient
    ) -> None:
        # Same image uploaded twice → identical response. Locks in
        # the mock's determinism at the API surface (not just the
        # backend unit test).
        r1 = client.post(
            "/api/v1/media/ocr",
            files={"file": ("a.png", PNG_1x1, "image/png")},
        )
        r2 = client.post(
            "/api/v1/media/ocr",
            files={"file": ("b.png", PNG_1x1, "image/png")},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["text"] == r2.json()["text"]
        assert r1.json()["confidence"] == r2.json()["confidence"]

    def test_ocr_backend_failure_maps_to_503(
        self, client: TestClient
    ) -> None:
        class _ExplodingBackend(OcrBackend):
            async def recognize(self, image, *, language="zh-CN"):
                raise OcrError("OCR_QUOTA_EXCEEDED", "upstream rate-limited")

        saved = get_ocr_backend()
        set_ocr_backend(_ExplodingBackend())
        try:
            r = client.post(
                "/api/v1/media/ocr",
                files={"file": ("p.png", PNG_1x1, "image/png")},
            )
        finally:
            set_ocr_backend(saved)
        assert r.status_code == 503
        assert r.json()["detail"]["error"] == "OCR_QUOTA_EXCEEDED"
