"""Tests for BE-AUTH-001-rollout — every protected route category
must reject 401 without a Bearer token.

These tests deliberately UNSET NIKI_TEST_FAKE_AUTH so the real
JWT validation path runs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.auth.jwt_service import issue_access_token
from niki.auth.sms_service import InMemorySMSCodeService, set_sms_service
from niki.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """No fake auth override — these tests exercise the real JWT path."""
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    set_sms_service(InMemorySMSCodeService())
    return TestClient(create_app())


# One endpoint from each protected category.
PROTECTED_ROUTES = [
    ("GET", "/api/v1/dashboard/alerts/child-abc"),
    ("GET", "/api/v1/dashboard/interest/child-abc"),
    ("GET", "/api/v1/dashboard/weekly-report/child-abc"),
    ("GET", "/api/v1/dashboard/learning/child-abc"),
    ("GET", "/api/v1/memory/short-term/abc"),
    ("GET", "/api/v1/memory/graph/entities"),
    ("GET", "/api/v1/emotion/baseline"),
    ("GET", "/api/v1/emotion/good-night"),
    ("GET", "/api/v1/treehole/summaries/child-abc"),
    ("GET", "/api/v1/treehole/store-stats"),
    ("GET", "/api/v1/policy/age/child-abc"),
    ("GET", "/api/v1/policy/blacklist/fam-abc"),
    ("GET", "/api/v1/tools/matrix"),
    ("GET", "/api/v1/tools/allowed/tutor"),
    ("GET", "/api/v1/session/stats/child-abc"),
    ("GET", "/api/v1/session/should-warn/child-abc"),
    ("GET", "/api/v1/session/is-exhausted/child-abc"),
]


class TestProtectedRoutesNoToken:
    @pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
    def test_returns_401_without_token(self, client: TestClient, method: str, path: str) -> None:
        resp = client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"
        body = resp.json()
        assert body["error"]["code"] == "MISSING_TOKEN"

    @pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
    def test_returns_401_with_garbage_token(
        self, client: TestClient, method: str, path: str
    ) -> None:
        resp = client.request(method, path, headers={"Authorization": "Bearer not-a-jwt"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "MALFORMED_TOKEN"


class TestProtectedRoutesWrongScheme:
    @pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
    def test_returns_401_with_basic_auth(self, client: TestClient, method: str, path: str) -> None:
        resp = client.request(
            method,
            path,
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401


class TestProtectedRoutesWithValidToken:
    """A real Bearer token passes the dep; downstream may 200 / 4xx / 5xx,
    but never 401/403."""

    @pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
    def test_token_passes_auth_check(self, client: TestClient, method: str, path: str) -> None:
        token = issue_access_token("parent_test", ttl_seconds=60)
        resp = client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
        )
        # Whatever the endpoint does with the parent, auth must NOT have
        # failed — so status is not 401 / 403.
        assert resp.status_code not in (401, 403), (
            f"{method} {path} returned {resp.status_code} with valid token"
        )


class TestMediaUploadAuth:
    def test_upload_401_without_token(self, client: TestClient) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 50
        resp = client.post(
            "/api/v1/media/upload",
            files={"file": ("a.png", png, "image/png")},
        )
        assert resp.status_code == 401

    def test_upload_with_token_proceeds(self, client: TestClient, tmp_path) -> None:
        from niki.media.storage import (
            LocalDiskStorage,
            set_media_storage,
        )

        set_media_storage(LocalDiskStorage(tmp_path))
        token = issue_access_token("parent_test", ttl_seconds=60)
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 50
        resp = client.post(
            "/api/v1/media/upload",
            files={"file": ("a.png", png, "image/png")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200


class TestPublicRoutesUnchanged:
    """Sanity: the rollout must NOT have touched public endpoints."""

    @pytest.mark.parametrize(
        "path",
        [
            "/health",
            "/auth/consent/document/parent",
            "/auth/consent/document/child",
            "/openapi.json",
        ],
    )
    def test_public_endpoint_no_auth_required(self, client: TestClient, path: str) -> None:
        resp = client.get(path)
        # Should not 401 (some paths 200/404 if resource missing, but
        # never auth-rejected).
        assert resp.status_code != 401, f"{path} unexpectedly required auth"
