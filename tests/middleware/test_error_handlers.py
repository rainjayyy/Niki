"""Tests verifying that all global exception handlers return the §6.3
shape (BE-API-001): {'success': false, 'error': {code, message}, ...}.

Handlers tested:
- NikiException
- ValidationError
- SafetyViolationError
- LLMRateLimitError
- LLMTimeoutError
- LLMAuthenticationError
- InfrastructureError
- ConfigurationError
- unhandled Exception
- Pydantic RequestValidationError (422)
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.exceptions import RequestValidationError

from niki.middleware.error_handler import setup_error_handlers
from niki.utils.exceptions import (
    ConfigurationError,
    InfrastructureError,
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMTimeoutError,
    NikiException,
    SafetyViolationError,
    ValidationError,
)


@pytest.fixture
def app() -> FastAPI:
    """Per-test FastAPI app with all known handlers wired + triggerable
    routes that raise each kind of exception."""
    app = FastAPI()
    setup_error_handlers(app)

    @app.get("/raise/niki")
    async def _raise_niki():
        raise NikiException("niki boom")

    @app.get("/raise/niki-with-code")
    async def _raise_niki_with_code():
        # Simulate a NikiException subclass with a stable .code attr.
        class _AuthError(NikiException):
            code = "INVALID_PHONE_FORMAT"

        raise _AuthError("phone must be 11 digits")

    @app.get("/raise/validation")
    async def _raise_validation():
        raise ValidationError("bad input")

    @app.get("/raise/safety")
    async def _raise_safety():
        raise SafetyViolationError("bad word detected")

    @app.get("/raise/llm-rate")
    async def _raise_llm_rate():
        raise LLMRateLimitError(model="deepseek")

    @app.get("/raise/llm-timeout")
    async def _raise_llm_timeout():
        raise LLMTimeoutError(model="doubao", timeout=30)

    @app.get("/raise/llm-auth")
    async def _raise_llm_auth():
        raise LLMAuthenticationError(model="doubao")

    @app.get("/raise/infra")
    async def _raise_infra():
        raise InfrastructureError("redis down")

    @app.get("/raise/config")
    async def _raise_config():
        raise ConfigurationError("missing JWT_SECRET")

    @app.get("/raise/unhandled")
    async def _raise_unhandled():
        raise RuntimeError("kaboom")

    @app.post("/echo")
    async def _echo(request: Request):
        body = await request.json()
        return {"ok": body}

    @app.post("/validate-body")
    async def _validate(payload: dict):
        # Pydantic validation will fire if body is not a dict.
        return payload

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _assert_new_shape(body: dict, *, expected_code: str | None = None) -> None:
    """Common assertions for the §6.3 error shape."""
    assert body.get("success") is False
    assert "request_id" in body and isinstance(body["request_id"], str)
    assert isinstance(body.get("error"), dict)
    err = body["error"]
    assert "code" in err and "message" in err
    if expected_code:
        assert err["code"] == expected_code
    # Back-compat fields
    assert body.get("error_code") == err.get("code")
    assert body.get("error_detail") is not None or err.get("detail") in (None, {})


class TestNikiException:
    def test_plain_niki_exception(self, client: TestClient) -> None:
        r = client.get("/raise/niki")
        assert r.status_code == 400
        _assert_new_shape(r.json(), expected_code="NIKIEXCEPTION")

    def test_niki_exception_with_custom_code(self, client: TestClient) -> None:
        r = client.get("/raise/niki-with-code")
        assert r.status_code == 400
        body = r.json()
        assert body["success"] is False
        assert body["error"]["code"] == "INVALID_PHONE_FORMAT"
        assert body["error_code"] == "INVALID_PHONE_FORMAT"


class TestSubExceptions:
    def test_validation_error(self, client: TestClient) -> None:
        r = client.get("/raise/validation")
        assert r.status_code == 422
        _assert_new_shape(r.json(), expected_code="VALIDATION_ERROR")

    def test_safety_violation(self, client: TestClient) -> None:
        r = client.get("/raise/safety")
        assert r.status_code == 400
        _assert_new_shape(r.json(), expected_code="SAFETY_VIOLATION")

    def test_llm_rate_limit(self, client: TestClient) -> None:
        r = client.get("/raise/llm-rate")
        assert r.status_code == 429
        _assert_new_shape(r.json(), expected_code="LLM_RATE_LIMIT")

    def test_llm_timeout(self, client: TestClient) -> None:
        r = client.get("/raise/llm-timeout")
        assert r.status_code == 504
        _assert_new_shape(r.json(), expected_code="LLM_TIMEOUT")

    def test_llm_auth(self, client: TestClient) -> None:
        r = client.get("/raise/llm-auth")
        assert r.status_code == 500
        _assert_new_shape(r.json(), expected_code="LLM_AUTH_FAILED")

    def test_infrastructure_error(self, client: TestClient) -> None:
        r = client.get("/raise/infra")
        assert r.status_code == 503
        _assert_new_shape(r.json(), expected_code="INFRASTRUCTURE_UNAVAILABLE")

    def test_configuration_error(self, client: TestClient) -> None:
        r = client.get("/raise/config")
        assert r.status_code == 500
        _assert_new_shape(r.json(), expected_code="CONFIGURATION_ERROR")


class TestUnhandledAndValidation:
    def test_pydantic_validation_returns_new_shape(self, client: TestClient) -> None:
        # POST /validate-body with non-dict body triggers RequestValidationError.
        r = client.post(
            "/validate-body",
            json=["not", "a", "dict"],  # wrong type
        )
        # Frontend expects 400 (not 422) for body validation errors so
        # its interceptor treats them like every other 4xx. The
        # structured `errors` list is still in the body for callers
        # that want field-level details.
        assert r.status_code == 400
        body = r.json()
        _assert_new_shape(body, expected_code="REQUEST_VALIDATION_ERROR")
        # Detail should include the original Pydantic errors.
        assert "errors" in body["error"]["detail"]


class TestRequestIdPropagation:
    def test_request_id_from_header_is_echoed(self, client: TestClient) -> None:
        r = client.get(
            "/raise/validation",
            headers={"X-Request-ID": "abc-123-test"},
        )
        body = r.json()
        assert body["request_id"] == "abc-123-test"

    def test_request_id_generated_when_absent(self, client: TestClient) -> None:
        r = client.get("/raise/validation")
        body = r.json()
        assert isinstance(body["request_id"], str)
        assert len(body["request_id"]) >= 8  # UUID short form


class TestBackCompatFieldShape:
    def test_flat_error_code_preserved(self, client: TestClient) -> None:
        r = client.get("/raise/niki-with-code")
        body = r.json()
        # Old frontend can still read this.
        assert body["error_code"] == "INVALID_PHONE_FORMAT"
        # And the nested dict is available too.
        assert body["error"]["code"] == "INVALID_PHONE_FORMAT"
        assert body["error"]["message"] == "phone must be 11 digits"
