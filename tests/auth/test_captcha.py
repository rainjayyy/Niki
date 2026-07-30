"""Tests for the captcha verification layer (BE-COMP-006).

Coverage:
  Unit (niki.auth.captcha.InMemoryCaptchaService):
    - challenge returns id + question with TTL
    - correct answer verifies
    - wrong answer rejects
    - expired challenge rejects
    - 5-min TTL respected (configuration-level)
    - service cap (1000 entries) drops oldest
    - challenge is single-use (replay rejects)
    - answers are SHA-256 hashed (no plaintext in memory)

  Endpoint (niki.api.auth):
    - GET /auth/captcha/challenge returns the trio
    - POST /auth/captcha/verify returns {valid: bool}
    - /auth/sms/send with valid captcha proceeds (dev flag off)
    - /auth/sms/send without captcha still works (dev flag off)
    - /auth/sms/send without captcha → 400 CAPTCHA_INVALID (flag on)
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import niki.api.auth as api_auth
from niki.auth.captcha import (
    InMemoryCaptchaService,
    set_captcha_service,
)
from niki.auth.lifecycle import AccountLifecycle
from niki.auth.sms_service import InMemorySMSCodeService, set_sms_service
from niki.config.settings import Settings, get_settings
from niki.main import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_question(question: str) -> int:
    """Solve the captcha's own arithmetic puzzle.

    We don't store the plaintext answer; tests read it back by
    re-evaluating the public question. Asserting on the parser
    is fair game — it's a fixture of the contract.
    """
    expr = question.replace("= ?", "").strip()
    return eval(expr)  # noqa: S307 — trusted local fixture


@pytest.fixture
def captcha_service() -> Iterator[InMemoryCaptchaService]:
    svc = InMemoryCaptchaService()
    set_captcha_service(svc)
    yield svc
    set_captcha_service(InMemoryCaptchaService())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    api_auth._lifecycle = AccountLifecycle()
    set_sms_service(InMemorySMSCodeService())
    set_captcha_service(InMemoryCaptchaService())
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


pytestmark_async = pytest.mark.asyncio


class TestCaptchaServiceUnit:
    pytestmark = pytestmark_async

    async def test_challenge_returns_id_and_question_with_ttl(
        self, captcha_service: InMemoryCaptchaService
    ) -> None:
        before = time.time()
        ch = await captcha_service.create_challenge()
        after = time.time()
        assert isinstance(ch.id, str) and len(ch.id) >= 16
        assert ch.question.endswith("= ?")
        # TTL ≈ 5 min; verify by checking the window falls inside it.
        assert before + 300 - 1 <= ch.expires_at <= after + 300 + 1
        # Plaintext answer is NOT exposed on the public dataclass.
        # The internal `_StoredCaptcha` is what holds the hash.
        assert not hasattr(ch, "answer") or ch.answer is None

    async def test_correct_answer_verifies(
        self, captcha_service: InMemoryCaptchaService
    ) -> None:
        ch = await captcha_service.create_challenge()
        answer = _parse_question(ch.question)
        assert await captcha_service.verify(ch.id, answer) is True

    async def test_wrong_answer_rejects(
        self, captcha_service: InMemoryCaptchaService
    ) -> None:
        ch = await captcha_service.create_challenge()
        answer = _parse_question(ch.question)
        assert await captcha_service.verify(ch.id, answer + 1) is False

    async def test_expired_challenge_rejects(
        self, captcha_service: InMemoryCaptchaService
    ) -> None:
        ch = await captcha_service.create_challenge()
        answer = _parse_question(ch.question)
        # Backdate expiry far enough that any clock granularity is
        # guaranteed to have crossed the boundary.
        captcha_service._records[ch.id].expires_at -= 1000
        assert await captcha_service.verify(ch.id, answer) is False

    async def test_ttl_is_configurable_to_5_minutes(self) -> None:
        """Default ctor → 300s TTL (matches SMS code TTL)."""
        svc = InMemoryCaptchaService()
        assert svc._ttl == 300

    async def test_cap_drops_oldest_entries(self) -> None:
        svc = InMemoryCaptchaService(cap=3)
        ids = []
        for _ in range(5):
            ch = await svc.create_challenge()
            ids.append(ch.id)
            # Tiny pause to make ordering deterministic; insert-order
            # is what we test, not wall-clock.
        # Only the last 3 should still be present.
        assert len(svc) == 3
        assert ids[0] not in svc._records
        assert ids[1] not in svc._records
        assert ids[2] in svc._records
        assert ids[3] in svc._records
        assert ids[4] in svc._records

    async def test_challenge_is_single_use(
        self, captcha_service: InMemoryCaptchaService
    ) -> None:
        ch = await captcha_service.create_challenge()
        answer = _parse_question(ch.question)
        assert await captcha_service.verify(ch.id, answer) is True
        # Replay must reject — otherwise an attacker who intercepted
        # the (id, answer) tuple gets unlimited SMS sends.
        assert await captcha_service.verify(ch.id, answer) is False

    async def test_answers_are_stored_as_hash_not_plaintext(
        self, captcha_service: InMemoryCaptchaService
    ) -> None:
        ch = await captcha_service.create_challenge()
        answer = _parse_question(ch.question)
        record = captcha_service._records[ch.id]
        # The stored field MUST be a hex sha256, never the plaintext.
        assert record.answer_hash == ch.answer_hash
        assert len(record.answer_hash) == 64  # sha256 hex digest
        assert str(answer) != record.answer_hash


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


class TestCaptchaEndpoints:
    def test_get_challenge_returns_id_and_question(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/captcha/challenge")
        assert resp.status_code == 200
        body = resp.json()
        assert "captcha_id" in body
        assert "question" in body
        assert "expires_at" in body
        assert body["question"].endswith("= ?")

    def test_post_verify_correct_answer_returns_true(self, client: TestClient) -> None:
        ch = client.get("/api/v1/auth/captcha/challenge").json()
        answer = _parse_question(ch["question"])
        resp = client.post(
            "/api/v1/auth/captcha/verify",
            json={"captcha_id": ch["captcha_id"], "answer": answer},
        )
        assert resp.status_code == 200
        assert resp.json() == {"valid": True}

    def test_post_verify_wrong_answer_returns_false(self, client: TestClient) -> None:
        ch = client.get("/api/v1/auth/captcha/challenge").json()
        resp = client.post(
            "/api/v1/auth/captcha/verify",
            json={"captcha_id": ch["captcha_id"], "answer": 99999},
        )
        assert resp.status_code == 200
        assert resp.json() == {"valid": False}


class TestSmsSendWithCaptcha:
    """Wiring tests: captcha ↔ /auth/sms/send."""

    def _enable_required_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the cached settings to require captcha."""
        settings = get_settings()
        # mutate in place — get_settings() is lru_cached so all callers
        # see the new value without rebuilding the singleton.
        monkeypatch.setattr(settings, "sms_captcha_required", True)

    def test_sms_send_without_captcha_works_in_dev_mode(
        self, client: TestClient
    ) -> None:
        """Default flag = False: existing SMS-send tests must still pass."""
        resp = client.post(
            "/api/v1/auth/sms/send", json={"phone": "13800138000"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"

    def test_sms_send_with_valid_captcha_proceeds(self, client: TestClient) -> None:
        ch = client.get("/api/v1/auth/captcha/challenge").json()
        answer = _parse_question(ch["question"])
        resp = client.post(
            "/api/v1/auth/sms/send",
            json={
                "phone": "13800138000",
                "captcha_id": ch["captcha_id"],
                "captcha_answer": answer,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"

    def test_sms_send_with_wrong_captcha_returns_400(
        self, client: TestClient
    ) -> None:
        ch = client.get("/api/v1/auth/captcha/challenge").json()
        resp = client.post(
            "/api/v1/auth/sms/send",
            json={
                "phone": "13800138000",
                "captcha_id": ch["captcha_id"],
                "captcha_answer": 99999,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "CAPTCHA_INVALID"

    def test_sms_send_without_captcha_in_prod_mode_returns_400(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._enable_required_flag(monkeypatch)
        resp = client.post(
            "/api/v1/auth/sms/send", json={"phone": "13800138000"}
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "CAPTCHA_INVALID"

    def test_sms_send_in_prod_mode_with_valid_captcha_succeeds(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._enable_required_flag(monkeypatch)
        ch = client.get("/api/v1/auth/captcha/challenge").json()
        answer = _parse_question(ch["question"])
        resp = client.post(
            "/api/v1/auth/sms/send",
            json={
                "phone": "13800138000",
                "captcha_id": ch["captcha_id"],
                "captcha_answer": answer,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"


# ---------------------------------------------------------------------------
# Settings sanity check
# ---------------------------------------------------------------------------


class TestSettingsFlag:
    def test_default_is_false(self) -> None:
        """Existing test suites depend on captcha being optional."""
        settings = Settings()
        assert settings.sms_captcha_required is False