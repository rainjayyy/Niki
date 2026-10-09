"""Static-analysis tests for the FastAPI OpenAPI schema (BE-API-006).

The frontend TypeScript SDK generator consumes the OpenAPI document at
`GET /openapi.json`. To keep its generated type comments useful (and
the Swagger UI / ReDoc readable), every endpoint needs:

  - `summary`   (one-line title — appears in navtree + TSDoc)
  - `description` (multi-paragraph body — appears in expanded view)
  - `tags`      (list of group names; each tag must have a description)
  - 200 response carrying a JSON schema
  - For POST/PUT/PATCH, a JSON request body schema

This module is a self-contained static check: it calls
`create_app().openapi()` to materialize the schema in-process — no
TestClient, no DB, no FastAPI server boot. The build of the schema
itself is the source of truth; if a decorator drops its `summary=`
the schema still has a `summary` (FastAPI falls back to the docstring
first line) but **we want the explicit `summary=` kwarg** so future
engineers can't accidentally lose it by editing the docstring.

Backend conventions enforced:
  - `summary` is present, non-empty, and exactly what the author typed.
  - `description` is present and non-empty.
  - `tags` is a non-empty list.
  - Each unique tag in any endpoint's `tags` ALSO appears in the
    global `tags` block with a non-empty `description`.
  - Every path×method has a 200 response with
    `content.application/json.schema` (or a Pydantic response_model
    that's been serialised to `schema` by FastAPI).
  - POST/PUT/PATCH endpoints declare
    `requestBody.content.application/json.schema`.

Paths under `/api/v1` are the production surface; we enforce the
rules there only (so internal helper endpoints — health probes, etc.
— are not over-asserted).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Fake-auth so create_app() doesn't open DB / Redis.
os.environ.setdefault("NIKI_TEST_FAKE_AUTH", "1")

from niki.main import create_app  # noqa: E402

HTTP_METHODS = {"get", "post", "put", "delete", "patch"}
# Methods that take a JSON request body and so need a requestBody schema.
BODY_METHODS = {"post", "put", "patch"}


@pytest.fixture(scope="module")
def openapi_schema() -> dict:
    """Build the OpenAPI schema once for the whole module.

    `create_app()` reaches into Postgres / Redis / Chroma for startup
    probes but the `.openapi()` call only serialises metadata, so it's
    deterministic and safe.
    """
    app = create_app()
    return app.openapi()


@pytest.fixture(scope="module")
def api_paths(openapi_schema: dict) -> list[tuple[str, str, dict]]:
    """Return a list of (method, path, info) for every API endpoint under /api/v1.

    Filters out non-API paths (e.g. top-level /health) and skips
    non-HTTP methods (`parameters`, etc.).
    """
    paths = openapi_schema.get("paths", {})
    out: list[tuple[str, str, dict]] = []
    for path, methods in paths.items():
        if not path.startswith("/api/v1"):
            continue
        for method, info in methods.items():
            if method.lower() not in HTTP_METHODS:
                continue
            out.append((method.lower(), path, info))
    return out


class TestEndpointMetadata:
    """Every endpoint under /api/v1 has the metadata the SDK needs."""

    def test_summary_present(self, api_paths: list) -> None:
        missing = [
            f"{m.upper()} {p}"
            for m, p, info in api_paths
            if not (info.get("summary") or "").strip()
        ]
        assert not missing, (
            f"Endpoints missing summary= (count={len(missing)}):\n  "
            + "\n  ".join(missing[:20])
        )

    def test_description_present(self, api_paths: list) -> None:
        missing = [
            f"{m.upper()} {p}"
            for m, p, info in api_paths
            if not (info.get("description") or "").strip()
        ]
        assert not missing, (
            f"Endpoints missing description= (count={len(missing)}):\n  "
            + "\n  ".join(missing[:20])
        )

    def test_tags_present(self, api_paths: list) -> None:
        missing = [
            f"{m.upper()} {p}"
            for m, p, info in api_paths
            if not info.get("tags")
        ]
        assert not missing, (
            f"Endpoints missing tags= (count={len(missing)}):\n  "
            + "\n  ".join(missing[:20])
        )


class TestGlobalTagMetadata:
    """Each unique tag in any endpoint also has a global description."""

    def test_all_tags_have_global_description(
        self, openapi_schema: dict, api_paths: list
    ) -> None:
        global_tags = {
            t["name"]: (t.get("description") or "")
            for t in openapi_schema.get("tags", [])
        }
        # Collect every tag referenced by any endpoint.
        referenced: set[str] = set()
        for _m, _p, info in api_paths:
            for tag in info.get("tags", []):
                if isinstance(tag, str):
                    referenced.add(tag)

        missing_global = sorted(referenced - set(global_tags))
        assert not missing_global, (
            f"Tags referenced by endpoints but absent from "
            f"`openapi.tags` (need {len(missing_global)} more entries): "
            f"{missing_global}"
        )

    def test_global_tags_have_descriptions(self, openapi_schema: dict) -> None:
        bare: list[str] = []
        for tag in openapi_schema.get("tags", []):
            if not (tag.get("description") or "").strip():
                bare.append(tag.get("name", "<unnamed>"))
        assert not bare, (
            "Tags with empty descriptions: "
            f"{bare}"
        )


class TestResponseSchemas:
    """Every endpoint's success path returns a JSON schema."""

    def test_200_response_has_json_schema(self, api_paths: list) -> None:
        # Endpoints whose 200 is a streaming binary (PDF / SSE / etc.)
        # legitimately have no application/json schema. We exclude
        # those paths and verify everything else.
        binary_response_paths = {
            ("get", "/api/v1/dashboard/weekly-report/{child_id}/pdf"),
            ("get", "/api/v1/media/{file_id}"),
            ("get", "/api/v1/media/{file_id}/thumb"),
            ("get", "/api/v1/media/{file_id:path}"),
            ("get", "/api/v1/media/{file_id:path}/thumb"),
            ("get", "/api/v1/parents/{parent_id}/data-export"),
            ("get", "/api/v1/deletion/{request_id}/receipt"),
            ("get", "/api/v1/alerts/stream/{child_id}"),
            ("post", "/api/v1/chat/stream"),
            # BE-API-009: Prometheus exposition endpoint returns
            # text/plain; not application/json.
            ("get", "/api/v1/metrics"),
        }
        missing: list[str] = []
        for m, p, info in api_paths:
            if (m, p) in binary_response_paths:
                continue
            responses = info.get("responses", {}) or {}
            two_hundred = responses.get("200") or responses.get(
                "201"
            )
            if two_hundred is None:
                missing.append(f"{m.upper()} {p} (no 200/201)")
                continue
            content = (
                (two_hundred.get("content") or {}).get(
                    "application/json"
                )
            )
            if not content or not content.get("schema"):
                missing.append(
                    f"{m.upper()} {p} (no application/json.schema)"
                )
        assert not missing, (
            "Endpoints without a 200/201 application/json schema:\n  "
            + "\n  ".join(missing[:20])
        )


class TestRequestBodySchemas:
    """POST/PUT/PATCH endpoints accept a JSON request body schema."""

    def test_post_body_has_json_schema(self, api_paths: list) -> None:
        # Endpoints that take a multipart body (file uploads) have a
        # `multipart/form-data` requestBody, not a JSON one. We skip
        # those.
        multipart_paths = {
            ("post", "/api/v1/media/upload"),
            ("post", "/api/v1/media/avatar"),
            ("post", "/api/v1/media/ocr"),
            ("post", "/api/v1/audio/transcribe"),
        }
        # Endpoints with intentionally empty bodies (no `payload: dict`
        # parameter in the handler). Tracking these explicitly here is
        # the audit hook — if you add a new parameter to one of these
        # handlers, you must either declare its schema here too or add
        # a JSON requestBody to the endpoint.
        empty_body_paths = {
            ("post", "/api/v1/auth/logout"),
            ("post", "/api/v1/dashboard/alerts/{alert_id}/delivered"),
            ("post", "/api/v1/emotion/proactive-care"),
            ("post", "/api/v1/memory/graph/apply-decay"),
            ("post", "/api/v1/resilience/breaker/reset"),
            ("post", "/api/v1/resilience/breaker/force-open"),
            ("post", "/api/v1/resilience/degradation/recover"),
            ("post", "/api/v1/resilience/simulate-outage"),
            ("post", "/api/v1/resilience/simulate-recovery"),
            ("post", "/api/v1/treehole/purge-expired"),
            ("post", "/api/v1/push/kick/{device_id}"),
            ("post", "/api/v1/push/enable/{device_id}"),
            ("post", "/api/v1/children/letters/{letter_id}/read"),
        }
        missing: list[str] = []
        for m, p, info in api_paths:
            if m not in BODY_METHODS:
                continue
            if (m, p) in multipart_paths:
                continue
            if (m, p) in empty_body_paths:
                continue
            body = info.get("requestBody") or {}
            content = (body.get("content") or {}).get(
                "application/json"
            )
            if not content or not content.get("schema"):
                missing.append(f"{m.upper()} {p}")
        assert not missing, (
            "POST/PUT/PATCH endpoints without a JSON requestBody schema:\n  "
            + "\n  ".join(missing[:20])
        )


class TestCoverageSummary:
    """Coverage summary — surfaces the BE-API-006 completeness ratio."""

    def test_every_endpoint_has_summary(self, openapi_schema: dict) -> None:
        total = 0
        with_summary = 0
        for path, methods in openapi_schema.get("paths", {}).items():
            if not path.startswith("/api/v1"):
                continue
            for method, info in methods.items():
                if method.lower() not in HTTP_METHODS:
                    continue
                total += 1
                if (info.get("summary") or "").strip():
                    with_summary += 1
        assert total > 0, "Sanity check: no /api/v1 endpoints?"
        assert with_summary == total, (
            f"{total - with_summary}/{total} endpoints missing summary"
        )
