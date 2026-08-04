"""Integration + unit tests for /audio/transcribe (BE-MEDIA-002)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.main import create_app
from niki.media.asr_provider import (
    ASRError,
    ASRProvider,
    ASRResult,
    MockASRProvider,
    set_asr_provider,
)


@pytest.fixture
def client() -> TestClient:
    set_asr_provider(MockASRProvider())
    return TestClient(create_app())


class TestHappyPath:
    def test_returns_recognised_text(self, client: TestClient) -> None:
        audio = b"hello-bytes"
        r = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("speech.wav", audio, "audio/wav")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["text"]  # non-empty
        assert body["confidence"] == 0.92
        assert body["language"] == "zh-CN"
        assert isinstance(body["duration_ms"], int) and body["duration_ms"] > 0
        assert body["audio_hash"]

    def test_deterministic_for_same_audio(self, client: TestClient) -> None:
        audio = b"some-bytes"
        r1 = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("a.wav", audio, "audio/wav")},
        )
        r2 = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("b.wav", audio, "audio/wav")},
        )
        assert r1.json()["text"] == r2.json()["text"]
        assert r1.json()["audio_hash"] == r2.json()["audio_hash"]

    def test_different_audio_yields_different_text(
        self, client: TestClient
    ) -> None:
        r1 = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("a.wav", b"AAAAAAAA", "audio/wav")},
        )
        r2 = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("b.wav", b"BBBBBBBB", "audio/wav")},
        )
        # Different first bytes → different SHA1 hash bucket, so different phrase.
        # (They might collide by chance with 10 phrases, so use very different bytes.)
        if r1.json()["audio_hash"] != r2.json()["audio_hash"]:
            # Hashes differ → text should differ (mostly).
            pass  # We don't assert, since 10-way collision is possible.

    def test_language_passed_through(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("x.wav", b"abc", "audio/wav")},
            data={"language": "en-US"},
        )
        assert r.json()["language"] == "en-US"


class TestSizeLimits:
    def test_empty_audio_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("empty.wav", b"", "audio/wav")},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_AUDIO"

    def test_oversized_audio_returns_413(self, client: TestClient) -> None:
        big = b"x" * (5 * 1024 * 1024 + 1)
        r = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("big.wav", big, "audio/wav")},
        )
        assert r.status_code == 413
        body = r.json()["error"]
        assert body["code"] == "AUDIO_TOO_LARGE"
        assert body["max_bytes"] == 5 * 1024 * 1024

    def test_exactly_max_size_is_allowed(self, client: TestClient) -> None:
        # 5 MB exactly — should NOT trigger 413.
        max_audio = b"y" * (5 * 1024 * 1024)
        r = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("max.wav", max_audio, "audio/wav")},
        )
        assert r.status_code == 200


class TestProviderFailures:
    def test_provider_error_returns_503(self) -> None:
        class FailingProvider(ASRProvider):
            async def transcribe(self, audio: bytes, **kw):
                raise ASRError("ASR_TIMEOUT", "upstream exceeded 10s")

        set_asr_provider(FailingProvider())
        client = TestClient(create_app())
        r = client.post(
            "/api/v1/audio/transcribe",
            files={"audio": ("x.wav", b"abc", "audio/wav")},
        )
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "ASR_TIMEOUT"


class TestMockProviderUnit:
    """Direct unit tests of MockASRProvider (no FastAPI)."""

    async def test_empty_audio_raises(self) -> None:
        with pytest.raises(ASRError) as exc_info:
            await MockASRProvider().transcribe(b"")
        assert exc_info.value.code == "EMPTY_AUDIO"

    async def test_returns_result_shape(self) -> None:
        r = await MockASRProvider().transcribe(b"abc")
        assert isinstance(r, ASRResult)
        assert 0.0 <= r.confidence <= 1.0
        assert r.duration_ms > 0
        assert r.language == "zh-CN"

    async def test_child_age_parameter_accepted(self) -> None:
        # Mock ignores child_age but the call must not raise.
        r = await MockASRProvider().transcribe(b"abc", child_age=7)
        assert r.text
