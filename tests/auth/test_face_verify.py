"""Tests for face verification."""

import pytest

from niki.auth.account import AuthError
from niki.auth.face_verify import FaceVerifier


@pytest.fixture
def verifier() -> FaceVerifier:
    return FaceVerifier()


class TestFaceVerification:
    async def test_valid_jpg_url(self, verifier: FaceVerifier) -> None:
        result = await verifier.verify("https://example.com/face.jpg")
        assert result.verified
        assert result.confidence > 0.8
        assert result.liveness_score > 0.7
        assert result.verified_at is not None

    async def test_valid_png_url(self, verifier: FaceVerifier) -> None:
        result = await verifier.verify("https://example.com/face.png")
        assert result.verified

    async def test_invalid_url_format_raises(self, verifier: FaceVerifier) -> None:
        with pytest.raises(AuthError) as exc_info:
            await verifier.verify("not-a-url")
        assert exc_info.value.code == "INVALID_IMAGE_URL"

    async def test_empty_url_raises(self, verifier: FaceVerifier) -> None:
        with pytest.raises(AuthError) as exc_info:
            await verifier.verify("")
        assert exc_info.value.code == "INVALID_IMAGE_URL"

    async def test_non_image_extension_fails(self, verifier: FaceVerifier) -> None:
        result = await verifier.verify("https://example.com/file.txt")
        assert not result.verified

    async def test_with_reference_image(self, verifier: FaceVerifier) -> None:
        result = await verifier.verify(
            live_image_url="https://example.com/live.jpg",
            reference_image_url="https://example.com/id.jpg",
        )
        assert result.verified

    async def test_invalid_reference_url_raises(self, verifier: FaceVerifier) -> None:
        with pytest.raises(AuthError) as exc_info:
            await verifier.verify(
                live_image_url="https://example.com/face.jpg",
                reference_image_url="not-a-url",
            )
        assert exc_info.value.code == "INVALID_IMAGE_URL"

    async def test_result_to_dict(self, verifier: FaceVerifier) -> None:
        result = await verifier.verify("https://example.com/face.jpg")
        d = result.to_dict()
        assert "verified" in d
        assert "confidence" in d
        assert "liveness_score" in d
        assert "provider" in d
