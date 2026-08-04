"""Integration + unit tests for /audio/synthesize (BE-MEDIA-003)."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from niki.main import create_app
from niki.media.tts_provider import (
    MockTTSProvider,
    TTSError,
    TTSProvider,
    set_tts_provider,
)


@pytest.fixture
def client() -> TestClient:
    set_tts_provider(MockTTSProvider())
    return TestClient(create_app())


class TestHappyPath:
    def test_returns_base64_audio_wav(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/audio/synthesize", json={"text": "Niki 你好"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["mime_type"] == "audio/wav"
        assert body["voice"] == "child_friendly"
        # base64 round-trip
        wav = base64.b64decode(body["audio_base64"], validate=True)
        assert wav.startswith(b"RIFF")
        assert b"WAVE" in wav[:12]
        assert b"data" in wav
        assert body["size_bytes"] == len(wav)

    def test_deterministic_for_same_text(self, client: TestClient) -> None:
        b1 = client.post(
            "/api/v1/audio/synthesize", json={"text": "hello"}
        ).json()
        b2 = client.post(
            "/api/v1/audio/synthesize", json={"text": "hello"}
        ).json()
        assert b1["audio_base64"] == b2["audio_base64"]
        assert b1["text_hash"] == b2["text_hash"]

    def test_different_text_yields_different_audio(
        self, client: TestClient
    ) -> None:
        b1 = client.post(
            "/api/v1/audio/synthesize", json={"text": "短的"}
        ).json()
        b2 = client.post(
            "/api/v1/audio/synthesize",
            json={"text": "一段明显更长的文本用于触发不同时长"},
        ).json()
        assert b1["text_hash"] != b2["text_hash"]
        # Different char counts → different durations → different sizes.
        assert b1["size_bytes"] != b2["size_bytes"]

    def test_speed_affects_output(self, client: TestClient) -> None:
        slow = client.post(
            "/api/v1/audio/synthesize",
            json={"text": "测试一二三四五六七八九十", "speed": 0.5},
        ).json()
        fast = client.post(
            "/api/v1/audio/synthesize",
            json={"text": "测试一二三四五六七八九十", "speed": 2.0},
        ).json()
        # Slower speed -> longer duration -> more bytes
        assert slow["duration_ms"] > fast["duration_ms"]
        assert slow["size_bytes"] > fast["size_bytes"]

    def test_voice_passed_through(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/audio/synthesize",
            json={"text": "hi", "voice": "gentle"},
        )
        assert r.json()["voice"] == "gentle"


class TestInputLimits:
    def test_empty_text_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/audio/synthesize", json={"text": ""})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_TEXT"

    def test_whitespace_only_text_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/audio/synthesize", json={"text": "   "})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_TEXT"

    def test_missing_text_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/audio/synthesize", json={})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_TEXT"

    def test_non_string_text_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/audio/synthesize", json={"text": 123})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_TEXT"

    def test_too_long_text_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/audio/synthesize", json={"text": "x" * 1001}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "TEXT_TOO_LONG"
        assert r.json()["error"]["max_chars"] == 1000

    def test_exactly_max_length_allowed(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/audio/synthesize", json={"text": "x" * 1000}
        )
        assert r.status_code == 200


class TestProviderFailures:
    def test_provider_error_returns_503(self) -> None:
        class BadProvider(TTSProvider):
            async def synthesize(self, text, **kw):
                raise TTSError("TTS_QUOTA", "upstream quota exhausted")

        set_tts_provider(BadProvider())
        client = TestClient(create_app())
        r = client.post(
            "/api/v1/audio/synthesize", json={"text": "hi"}
        )
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "TTS_QUOTA"


class TestMockProviderUnit:
    async def test_empty_text_raises(self) -> None:
        with pytest.raises(TTSError) as exc_info:
            await MockTTSProvider().synthesize("")
        assert exc_info.value.code == "EMPTY_TEXT"

    async def test_returns_valid_wav(self) -> None:
        r = await MockTTSProvider().synthesize("hello")
        assert r.audio.startswith(b"RIFF")
        assert b"WAVE" in r.audio[:12]
        assert b"data" in r.audio
        assert r.mime_type == "audio/wav"
        assert r.duration_ms > 0

    async def test_zero_chars_still_produces_audio(self) -> None:
        # Non-empty after .strip() should be required, but stripped empty
        # input raises; non-empty is fine even if short.
        r = await MockTTSProvider().synthesize("x")
        assert r.duration_ms >= 500  # min floor
