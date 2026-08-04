"""Integration + unit tests for /chat/stream SSE (BE-RT-001)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from niki.infrastructure.llm.streaming import (
    MockStreamingProvider,
    StreamingLLMProvider,
    set_streaming_provider,
)
from niki.main import create_app


@pytest.fixture
def client_with_fast_mock() -> TestClient:
    """Per-test fast mock to keep wall-clock low."""
    class FastMock(MockStreamingProvider):
        def __init__(self):
            super().__init__(delay_seconds=0.005, words_per_chunk=2)

    set_streaming_provider(FastMock())
    return TestClient(create_app())


def _iter_data_lines(resp) -> list[str]:
    """Yield every `data: <json>` payload from an SSE response."""
    out = []
    for line in resp.iter_lines():
        if line.startswith("data: "):
            out.append(line[6:])
    return out


def _parse_chat_stream(resp) -> tuple[list[str], dict | None, bool]:
    """Parse a stream response and return (chunks, final_meta, got_done)."""
    chunks: list[str] = []
    final: dict | None = None
    got_done = False
    for line in resp.iter_lines():
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            got_done = True
            continue
        obj = json.loads(payload)
        if "chunk" in obj:
            chunks.append(obj["chunk"])
        elif obj.get("is_final"):
            final = obj
    return chunks, final, got_done


class TestStreamHappyPath:
    def test_response_is_event_stream(self, client_with_fast_mock: TestClient) -> None:
        with client_with_fast_mock.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"text": "你好"},
        ) as r:
            assert r.status_code == 200
            ct = r.headers.get("content-type", "")
            assert ct.startswith("text/event-stream")

    def test_yields_multiple_chunks_then_done(
        self, client_with_fast_mock: TestClient
    ) -> None:
        with client_with_fast_mock.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"text": "恐龙为什么会灭绝？", "session_id": "abc"},
        ) as r:
            chunks, final, got_done = _parse_chat_stream(r)
        assert got_done is True
        assert len(chunks) >= 2  # at least 2 chunks for words_per_chunk=2
        # final event present
        assert final is not None
        assert final["session_id"] == "abc"
        assert final["is_final"] is True
        # final total_text equals concatenation of chunks
        assert final["total_text"] == "".join(chunks)

    def test_deterministic_for_same_input(
        self, client_with_fast_mock: TestClient
    ) -> None:
        def _run_once(text: str) -> str:
            with client_with_fast_mock.stream(
                "POST",
                "/api/v1/chat/stream",
                json={"text": text, "child_age": 7},
            ) as r:
                _, final, _ = _parse_chat_stream(r)
            return final["total_text"]

        assert _run_once("恐龙") == _run_once("恐龙")

    def test_token_indexes_are_monotonic(
        self, client_with_fast_mock: TestClient
    ) -> None:
        with client_with_fast_mock.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"text": "hello"},
        ) as r:
            indexes = []
            for line in r.iter_lines():
                if line.startswith("data: "):
                    payload = line[6:]
                    if payload == "[DONE]":
                        continue
                    obj = json.loads(payload)
                    if "token_index" in obj:
                        indexes.append(obj["token_index"])
        # Strictly increasing
        assert indexes == sorted(indexes)
        assert len(set(indexes)) == len(indexes)  # strictly non-overlapping


class TestStreamErrors:
    def test_empty_text_returns_400(self, client_with_fast_mock: TestClient) -> None:
        r = client_with_fast_mock.post(
            "/api/v1/chat/stream", json={"text": ""}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_TEXT"

    def test_whitespace_text_returns_400(self, client_with_fast_mock: TestClient) -> None:
        r = client_with_fast_mock.post(
            "/api/v1/chat/stream", json={"text": "   \n  "}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_TEXT"

    def test_missing_text_returns_400(self, client_with_fast_mock: TestClient) -> None:
        r = client_with_fast_mock.post(
            "/api/v1/chat/stream", json={}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_TEXT"

    def test_too_long_text_returns_400(self, client_with_fast_mock: TestClient) -> None:
        r = client_with_fast_mock.post(
            "/api/v1/chat/stream", json={"text": "x" * 1001}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "TEXT_TOO_LONG"

    def test_non_string_text_returns_400(self, client_with_fast_mock: TestClient) -> None:
        r = client_with_fast_mock.post(
            "/api/v1/chat/stream", json={"text": 123}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EMPTY_TEXT"


class TestProviderUnit:
    async def test_mock_emits_at_least_one_chunk(self) -> None:
        provider = MockStreamingProvider(delay_seconds=0.001, words_per_chunk=2)
        chunks: list[str] = []
        async for c in provider.stream("hello", child_age=7, session_id="x"):
            chunks.append(c)
        assert len(chunks) >= 1
        joined = " ".join(chunks)
        assert joined  # non-empty aggregate

    async def test_mock_skips_when_persona_set(self) -> None:
        # Deterministic by len(prompt)+child_age; same input -> same chunks.
        provider = MockStreamingProvider(delay_seconds=0.001)
        c1: list[str] = []
        async for c in provider.stream("abc", child_age=5):
            c1.append(c)
        c2: list[str] = []
        async for c in provider.stream("abc", child_age=5):
            c2.append(c)
        assert c1 == c2

    async def test_higher_words_per_chunk_yields_fewer_chunks(self) -> None:
        # Same text, larger chunk size ⇒ fewer chunks; same total.
        slow = MockStreamingProvider(delay_seconds=0.001, words_per_chunk=1)
        fast = MockStreamingProvider(delay_seconds=0.001, words_per_chunk=5)
        s: list[str] = []
        async for c in slow.stream("prompt"):
            s.append(c)
        f: list[str] = []
        async for c in fast.stream("prompt"):
            f.append(c)
        assert len(s) > len(f)
        assert "".join(s).replace(" ", "") == "".join(f).replace(" ", "")
