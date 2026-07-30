"""Tests for the TypeScript SDK generator (BE-API-007).

Two layers, mirroring the ticket's §5:

1. **Real `tsc --noEmit`** when a TypeScript compiler happens to be on
   PATH (`tsc`, or `npx --no-install tsc`). Neither this machine nor CI
   currently ships a node toolchain, so that test skips by default and
   auto-upgrades to a real compile the moment node is installed.
2. **Structural assertions** that always run: balanced braces, every
   exported function has a `Promise<...>` return annotation, no `any`,
   every imported symbol is actually exported by its target module,
   unique method names, full operation coverage, one file per tag.

Everything is driven off the live app schema plus small hand-written
fake schemas — `build_sdk()` is a pure function (OpenAPI dict in,
`{filename: text}` out), so the edge-case tests don't boot FastAPI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# Fake-auth so create_app() doesn't open DB / Redis (matches
# tests/api/test_openapi_docs.py).
os.environ.setdefault("NIKI_TEST_FAKE_AUTH", "1")

from scripts import gen_ts_sdk  # noqa: E402
from scripts.gen_ts_sdk import (  # noqa: E402
    HTTP_METHODS,
    ModelRegistry,
    build_sdk,
    camel_case,
    collect_operations,
    load_schema,
    main,
    pascal_case,
    slugify,
    strip_timestamp,
    tag_name,
    ts_type,
)

RUNTIME_FILES = {"client.ts", "models.ts", "index.ts"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    """The live OpenAPI document, built in-process (no server, no DB)."""
    return load_schema()


@pytest.fixture(scope="module")
def sdk(schema: dict[str, Any]) -> dict[str, str]:
    """Generated files as `{filename: contents}`, without a timestamp."""
    return build_sdk(schema, timestamp=None)


@pytest.fixture(scope="module")
def operations(schema: dict[str, Any]) -> list[Any]:
    models = ModelRegistry((schema.get("components") or {}).get("schemas"))
    return collect_operations(schema, models)


@pytest.fixture(scope="module")
def expected_tags(schema: dict[str, Any]) -> set[str]:
    """First tag of every operation — one output file each."""
    tags: set[str] = set()
    for methods in (schema.get("paths") or {}).values():
        for method, info in methods.items():
            if method.lower() not in HTTP_METHODS:
                continue
            names = [tag_name(t) for t in (info.get("tags") or [])]
            tags.add(next((n for n in names if n), "default"))
    return tags


@pytest.fixture(scope="module")
def expected_operation_count(schema: dict[str, Any]) -> int:
    return sum(
        1
        for methods in (schema.get("paths") or {}).values()
        for method in methods
        if method.lower() in HTTP_METHODS
    )


def exported_functions(source: str) -> list[str]:
    return re.findall(r"^export async function (\w+)\(", source, re.MULTILINE)


def strip_comments(source: str) -> str:
    """Drop comments so structural checks don't trip over English prose.

    Endpoint descriptions come from Python docstrings, which legitimately
    contain unbalanced parens ("(PIPL Art. 44)" wrapping across a truncation)
    and words like `True`. Only real code should be asserted on.
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", without_block, flags=re.MULTILINE)


def code_of(sdk: dict[str, str]) -> dict[str, str]:
    return {name: strip_comments(body) for name, body in sdk.items()}


def tag_modules(sdk: dict[str, str]) -> dict[str, str]:
    return {name: body for name, body in sdk.items() if name not in RUNTIME_FILES}


# ---------------------------------------------------------------------------
# The script runs
# ---------------------------------------------------------------------------


class TestScriptRuns:
    """AC-1: the generator runs clean against the current app."""

    def test_generates_files(self, sdk: dict[str, str]) -> None:
        assert sdk, "generator produced no files"
        for name, content in sdk.items():
            assert name.endswith(".ts"), name
            assert content.strip(), f"{name} is empty"

    def test_writes_to_disk_via_main(self, tmp_path: Path) -> None:
        out = tmp_path / "api"
        assert main(["--out", str(out), "--no-timestamp"]) == 0
        written = sorted(p.name for p in out.glob("*.ts"))
        assert "index.ts" in written
        assert "client.ts" in written
        assert "models.ts" in written
        assert len(written) > 10

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        out = tmp_path / "api"
        assert main(["--out", str(out), "--dry-run"]) == 0
        assert not out.exists()

    def test_runs_as_module_subprocess(self, tmp_path: Path) -> None:
        """`python -m scripts.gen_ts_sdk` works from the repo root."""
        out = tmp_path / "api"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.gen_ts_sdk",
                "--out",
                str(out),
                "--no-timestamp",
            ],
            cwd=str(gen_ts_sdk.ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert result.returncode == 0, result.stderr
        assert (out / "index.ts").exists()

    def test_no_external_python_imports(self) -> None:
        """AC-1: stdlib + FastAPI only — no new third-party dependency."""
        source = Path(gen_ts_sdk.__file__).read_text(encoding="utf-8")
        imported = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.M))
        roots = {name.split(".")[0] for name in imported}
        allowed = {
            "__future__",
            "argparse",
            "dataclasses",
            "datetime",
            "json",
            "niki",
            "pathlib",
            "re",
            "sys",
            "typing",
        }
        assert roots <= allowed, f"unexpected imports: {sorted(roots - allowed)}"


# ---------------------------------------------------------------------------
# Coverage: tags and operations
# ---------------------------------------------------------------------------


class TestCoverage:
    """AC-2 / AC-7: every tag gets a file, every operation gets a method."""

    def test_every_tag_has_a_file(self, sdk: dict[str, str], expected_tags: set[str]) -> None:
        produced = set(tag_modules(sdk))
        missing = {tag for tag in expected_tags if f"{slugify(tag)}.ts" not in produced}
        assert not missing, f"tags without an output file: {sorted(missing)}"

    def test_no_extra_files(self, sdk: dict[str, str], expected_tags: set[str]) -> None:
        expected_files = {f"{slugify(tag)}.ts" for tag in expected_tags}
        assert set(tag_modules(sdk)) == expected_files

    def test_every_tag_file_has_at_least_one_method(self, sdk: dict[str, str]) -> None:
        empty = [name for name, body in tag_modules(sdk).items() if not exported_functions(body)]
        assert not empty, f"tag modules with no methods: {empty}"

    def test_all_operations_present(
        self, sdk: dict[str, str], expected_operation_count: int
    ) -> None:
        total = sum(len(exported_functions(body)) for body in tag_modules(sdk).values())
        assert total == expected_operation_count

    def test_every_path_and_method_appears(
        self, sdk: dict[str, str], schema: dict[str, Any]
    ) -> None:
        combined = "\n".join(tag_modules(sdk).values())
        missing: list[str] = []
        for path, methods in (schema.get("paths") or {}).items():
            for method in methods:
                if method.lower() not in HTTP_METHODS:
                    continue
                if f"`{method.upper()} {path}`" not in combined:
                    missing.append(f"{method.upper()} {path}")
        assert not missing, f"operations missing from output: {missing[:10]}"

    def test_method_names_unique(self, sdk: dict[str, str]) -> None:
        names: list[str] = []
        for body in tag_modules(sdk).values():
            names.extend(exported_functions(body))
        duplicates = {n for n in names if names.count(n) > 1}
        assert not duplicates, f"duplicate method names: {sorted(duplicates)}"

    def test_index_reexports_everything(self, sdk: dict[str, str]) -> None:
        """AC-5: the barrel file re-exports every module."""
        index = sdk["index.ts"]
        for name in sdk:
            if name == "index.ts":
                continue
            stem = name[: -len(".ts")]
            assert f'export * from "./{stem}";' in index, f"index.ts misses {stem}"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """AC-6: identical output on repeat runs, modulo the timestamp."""

    def test_build_twice_identical(self, schema: dict[str, Any]) -> None:
        assert build_sdk(schema, timestamp=None) == build_sdk(schema, timestamp=None)

    def test_timestamp_is_the_only_difference(self, schema: dict[str, Any]) -> None:
        first = build_sdk(schema, timestamp="2020-01-01T00:00:00Z")
        second = build_sdk(schema, timestamp="2031-12-31T23:59:59Z")
        assert first != second, "timestamp should appear in the output"
        assert {k: strip_timestamp(v) for k, v in first.items()} == {
            k: strip_timestamp(v) for k, v in second.items()
        }

    def test_written_files_identical_across_runs(self, tmp_path: Path) -> None:
        first, second = tmp_path / "one", tmp_path / "two"
        assert main(["--out", str(first), "--no-timestamp"]) == 0
        assert main(["--out", str(second), "--no-timestamp"]) == 0
        left = {p.name: p.read_bytes() for p in first.glob("*.ts")}
        right = {p.name: p.read_bytes() for p in second.glob("*.ts")}
        assert left == right

    def test_output_independent_of_dict_order(self, schema: dict[str, Any]) -> None:
        """Reversing `paths` iteration order must not change the output."""
        shuffled = dict(schema)
        shuffled["paths"] = dict(reversed(list((schema.get("paths") or {}).items())))
        assert build_sdk(shuffled, timestamp=None) == build_sdk(schema, timestamp=None)

    def test_check_mode_detects_fresh_and_stale(self, tmp_path: Path) -> None:
        out = tmp_path / "api"
        assert main(["--out", str(out), "--no-timestamp"]) == 0
        assert main(["--out", str(out), "--check"]) == 0
        (out / "index.ts").write_text("// tampered\n", encoding="utf-8")
        assert main(["--out", str(out), "--check"]) == 1


# ---------------------------------------------------------------------------
# Structural properties of the emitted TypeScript
# ---------------------------------------------------------------------------


class TestGeneratedTypeScript:
    """AC-3 / AC-8 fallback: the output looks like valid strict TS."""

    def test_balanced_delimiters(self, sdk: dict[str, str]) -> None:
        for name, body in code_of(sdk).items():
            for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
                assert body.count(opener) == body.count(closer), (
                    f"{name}: unbalanced {opener}{closer}"
                )

    def test_every_method_is_async_and_typed(self, sdk: dict[str, str]) -> None:
        for name, body in tag_modules(sdk).items():
            signatures = re.findall(
                r"^export async function \w+\((.*?)\): (\S+)",
                body,
                re.MULTILINE | re.DOTALL,
            )
            assert signatures, f"{name}: no methods found"
            for _args, return_type in signatures:
                assert return_type.startswith("Promise<"), (
                    f"{name}: return type {return_type!r} is not a Promise"
                )

    def test_no_any_type(self, sdk: dict[str, str]) -> None:
        for name, body in code_of(sdk).items():
            assert not re.search(r":\s*any\b", body), f"{name} uses `any`"

    def test_no_placeholders(self, sdk: dict[str, str]) -> None:
        """No Python repr or unfinished marker leaks into the emitted code."""
        for name, body in code_of(sdk).items():
            for marker in ("TODO", "FIXME", "XXX", "<class ", "None", "True", "False"):
                assert marker not in body, f"{name} leaks {marker!r}"

    def test_generated_banner_on_every_file(self, sdk: dict[str, str]) -> None:
        for name, body in sdk.items():
            assert body.startswith("/**"), f"{name} missing header comment"
            assert "AUTO-GENERATED" in body.split("*/")[0], f"{name} missing banner"
            assert "DO NOT EDIT" in body.split("*/")[0], f"{name} missing warning"

    def test_imports_resolve_to_real_exports(self, sdk: dict[str, str]) -> None:
        exports: dict[str, set[str]] = {}
        for name, body in sdk.items():
            stem = name[: -len(".ts")]
            found = set(
                re.findall(
                    r"^export (?:async function|function|class|interface|type|const) (\w+)",
                    body,
                    re.MULTILINE,
                )
            )
            exports[stem] = found

        for name, body in sdk.items():
            for symbols, target in re.findall(
                r"import(?: type)? \{([^}]*)\} from \"\./(\w[\w.-]*)\";", body
            ):
                wanted = {s.strip() for s in symbols.split(",") if s.strip()}
                available = exports.get(target, set())
                missing = wanted - available
                assert not missing, f"{name} imports {missing} missing from {target}.ts"

    def test_path_params_are_interpolated(self, sdk: dict[str, str]) -> None:
        """No `{child_id}` placeholder survives into a template literal."""
        for name, body in tag_modules(sdk).items():
            leftovers = re.findall(r"`/api/[^`]*\{(\w+)\}", body)
            assert not leftovers, f"{name}: un-interpolated path params {leftovers}"

    def test_path_params_are_encoded(self, sdk: dict[str, str]) -> None:
        combined = "\n".join(tag_modules(sdk).values())
        assert "encodeURIComponent" in combined

    def test_no_required_param_after_optional(self, sdk: dict[str, str]) -> None:
        """TS rejects `(a?: X, b: Y)`. Verify we never emit that."""
        for name, body in tag_modules(sdk).items():
            for args in re.findall(
                r"^export async function \w+\(\n(.*?)\n\): ", body, re.MULTILINE | re.DOTALL
            ):
                seen_optional = False
                for line in args.splitlines():
                    stripped = line.strip().rstrip(",")
                    if not stripped:
                        continue
                    optional = "?:" in stripped
                    if seen_optional:
                        assert optional or "| undefined" in stripped, (
                            f"{name}: required param {stripped!r} follows an optional one"
                        )
                    seen_optional = seen_optional or optional

    def test_client_exports_runtime_surface(self, sdk: dict[str, str]) -> None:
        client = sdk["client.ts"]
        for symbol in (
            "export interface RequestOptions",
            "export function configure",
            "export class ApiError",
            "export async function request",
            "export function buildUrl",
        ):
            assert symbol in client, f"client.ts missing {symbol!r}"

    def test_client_does_not_set_multipart_content_type(self, sdk: dict[str, str]) -> None:
        """The runtime must add the boundary itself for FormData bodies."""
        client = sdk["client.ts"]
        assert "multipart/form-data" not in client
        assert "instanceof FormData" in client

    def test_models_cover_component_schemas(
        self, sdk: dict[str, str], schema: dict[str, Any]
    ) -> None:
        """AC-4: every `components.schemas` entry becomes a TS declaration."""
        models = sdk["models.ts"]
        component_schemas = (schema.get("components") or {}).get("schemas") or {}
        for key in component_schemas:
            assert re.search(
                rf"^export (?:interface|type) {re.escape(key)}\b", models, re.MULTILINE
            ), f"models.ts missing declaration for {key}"

    def test_es2020_only_no_es5_shims(self, sdk: dict[str, str]) -> None:
        """AC: target is ES2020/Node 18+ — native fetch, no polyfills."""
        combined = "\n".join(sdk.values())
        assert "require(" not in combined
        assert "XMLHttpRequest" not in combined
        assert "axios" not in combined
        assert "doFetch" in sdk["client.ts"]


# ---------------------------------------------------------------------------
# tsc --noEmit (opportunistic)
# ---------------------------------------------------------------------------


def find_tsc() -> list[str] | None:
    """Locate a TypeScript compiler, or return None if there isn't one."""
    direct = shutil.which("tsc")
    if direct:
        return [direct]
    npx = shutil.which("npx")
    if npx:
        probe = subprocess.run(
            [npx, "--no-install", "tsc", "--version"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and "Version" in probe.stdout:
            return [npx, "--no-install", "tsc"]
    return None


class TestTypeScriptCompiles:
    """AC-8: real compile when a toolchain exists, skip otherwise."""

    def test_tsc_noemit(self, sdk: dict[str, str], tmp_path: Path) -> None:
        tsc = find_tsc()
        if tsc is None:
            pytest.skip("no TypeScript compiler on PATH (tsc / npx --no-install tsc)")

        src = tmp_path / "src"
        src.mkdir()
        for name, content in sdk.items():
            (src / name).write_text(content, encoding="utf-8", newline="\n")
        (tmp_path / "tsconfig.json").write_text(
            json.dumps(
                {
                    "compilerOptions": {
                        "target": "ES2020",
                        "module": "ESNext",
                        "moduleResolution": "bundler",
                        "lib": ["ES2020", "DOM"],
                        "strict": True,
                        "noEmit": True,
                        "skipLibCheck": True,
                        "types": [],
                    },
                    "include": ["src/**/*.ts"],
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [*tsc, "--noEmit", "--project", str(tmp_path / "tsconfig.json")],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"tsc failed:\n{result.stdout}\n{result.stderr}"


#: Exercises `client.ts` — the only generated file with behaviour — against an
#: injected fake `fetch`. Runs under node when a toolchain is available.
CLIENT_SMOKE_TS = """
import { configure, request, ApiError, buildQuery, buildUrl } from "./client";

const calls: Array<{ url: string; init: any }> = [];

function fakeFetch(url: any, init: any): Promise<Response> {
  calls.push({ url: String(url), init });
  if (String(url).includes("/boom")) {
    return Promise.resolve(new Response(
      JSON.stringify({
        success: false,
        error: { code: "BAD_PHONE", message: "nope" },
        request_id: "r1",
      }),
      { status: 400 },
    ));
  }
  if (String(url).includes("/empty")) {
    return Promise.resolve(new Response("", { status: 200 }));
  }
  return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
}

function check(condition: boolean, label: string): void {
  if (!condition) {
    failures.push(label);
  }
}

const failures: string[] = [];

async function main(): Promise<void> {
  configure({
    baseUrl: "https://api.test",
    getToken: () => "tok123",
    fetch: fakeFetch as any,
  });

  check(
    buildQuery({ a: 1, b: undefined, c: null, d: ["x", "y"] }) === "?a=1&d=x&d=y",
    "query skips undefined/null and expands arrays",
  );
  check(
    buildUrl("/api/v1/x", { p: 2 }) === "https://api.test/api/v1/x?p=2",
    "buildUrl joins baseUrl + path + query",
  );

  const ok = await request<{ ok: boolean }>("POST", "/api/v1/thing", { body: { a: 1 } });
  check(ok.ok === true, "JSON response is parsed");
  const headers = calls[0].init.headers as Record<string, string>;
  check(headers["Authorization"] === "Bearer tok123", "bearer token injected");
  check(headers["Content-Type"] === "application/json", "JSON content type set");
  check(calls[0].init.body === JSON.stringify({ a: 1 }), "object body serialised");

  let threw = false;
  try {
    await request("GET", "/api/v1/boom");
  } catch (caught) {
    threw = true;
    const err = caught as ApiError;
    check(err instanceof ApiError, "non-2xx throws ApiError");
    check(err.status === 400, "status preserved");
    check(err.code === "BAD_PHONE", "error code unwrapped from the 6.3 shape");
    check(err.message === "nope", "error message unwrapped");
    check(err.requestId === "r1", "request_id surfaced");
  }
  check(threw, "400 must throw");

  const empty = await request<void>("DELETE", "/api/v1/empty");
  check(empty === undefined, "empty body resolves to undefined");

  const form = new FormData();
  form.append("file", new Blob(["x"]), "a.txt");
  await request("POST", "/api/v1/upload", { body: form });
  const formHeaders = calls[calls.length - 1].init.headers as Record<string, string>;
  check(
    !("Content-Type" in formHeaders),
    "FormData must not set Content-Type (runtime adds the boundary)",
  );
  check(calls[calls.length - 1].init.body === form, "FormData passed through as-is");

  if (failures.length === 0) {
    console.log("SMOKE OK");
  } else {
    console.log("SMOKE FAILURES: " + failures.join(" | "));
  }
}

main();
"""


class TestClientRuntimeBehaviour:
    """Execute `client.ts` under node — the generated runtime actually works.

    Structural assertions can't prove that the bearer header lands in the
    right place or that a §6.3 error body becomes a populated `ApiError`.
    This compiles the runtime plus a driver script and runs it, so the
    behavioural contract the frontend depends on is checked end to end.
    Skips when there is no node/tsc toolchain.
    """

    def test_runtime_contract(self, sdk: dict[str, str], tmp_path: Path) -> None:
        tsc = find_tsc()
        node = shutil.which("node")
        if tsc is None or node is None:
            pytest.skip("needs both a TypeScript compiler and node on PATH")

        (tmp_path / "client.ts").write_text(sdk["client.ts"], encoding="utf-8", newline="\n")
        (tmp_path / "smoke.ts").write_text(CLIENT_SMOKE_TS, encoding="utf-8", newline="\n")
        compile_result = subprocess.run(
            [
                *tsc,
                "smoke.ts",
                "client.ts",
                "--target",
                "ES2020",
                "--module",
                "commonjs",
                "--strict",
                "--skipLibCheck",
                "--outDir",
                "out",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert compile_result.returncode == 0, (
            f"smoke compile failed:\n{compile_result.stdout}\n{compile_result.stderr}"
        )

        run_result = subprocess.run(
            [node, str(tmp_path / "out" / "smoke.js")],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert run_result.returncode == 0, (
            f"runtime assertions failed:\n{run_result.stdout}\n{run_result.stderr}"
        )
        assert "SMOKE OK" in run_result.stdout, run_result.stdout


# ---------------------------------------------------------------------------
# Pure-function unit tests (no app boot)
# ---------------------------------------------------------------------------


MINIMAL_SCHEMA: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Fake", "version": "1"},
    "paths": {
        "/api/v1/widgets/{widget_id}": {
            "get": {
                "tags": [{"name": "widgets", "description": "Widget things."}],
                "summary": "Fetch one widget",
                "description": "Fetch one widget by id.",
                "parameters": [
                    {
                        "name": "widget_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "verbose",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "boolean"},
                    },
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Widget"}}
                        }
                    }
                },
            },
            "delete": {
                "tags": ["widgets"],
                "summary": "Delete a widget",
                "responses": {"204": {"description": "No Content"}},
            },
        }
    },
    "components": {
        "schemas": {
            "Widget": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "size": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            }
        }
    },
}


class TestNamingHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("weekly_report", "weeklyReport"),
            ("child-id", "childId"),
            ("HealthResponse", "healthResponse"),
            ("", ""),
        ],
    )
    def test_camel_case(self, raw: str, expected: str) -> None:
        assert camel_case(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("content-policy", "content-policy"), ("auth_audit", "auth-audit"), ("", "misc")],
    )
    def test_slugify(self, raw: str, expected: str) -> None:
        assert slugify(raw) == expected

    def test_pascal_case(self) -> None:
        assert pascal_case("weekly report") == "WeeklyReport"

    def test_tag_name_accepts_str_and_dict(self) -> None:
        assert tag_name("auth") == "auth"
        assert tag_name({"name": "auth", "description": "x"}) == "auth"
        assert tag_name(42) == ""


class TestTypeMapping:
    @pytest.mark.parametrize(
        ("node", "expected"),
        [
            ({"type": "string"}, "string"),
            ({"type": "integer"}, "number"),
            ({"type": "number"}, "number"),
            ({"type": "boolean"}, "boolean"),
            ({"type": "null"}, "null"),
            ({"type": "array", "items": {"type": "string"}}, "Array<string>"),
            ({"type": "object", "additionalProperties": True}, "Record<string, unknown>"),
            ({"enum": ["a", "b"]}, '"a" | "b"'),
            ({"anyOf": [{"type": "string"}, {"type": "null"}]}, "string | null"),
            ({"type": "string", "format": "binary"}, "Blob"),
            ({}, "unknown"),
        ],
    )
    def test_scalar_and_composite(self, node: dict[str, Any], expected: str) -> None:
        assert ts_type(node, ModelRegistry({})) == expected

    def test_ref_resolves_to_interface_name(self) -> None:
        models = ModelRegistry({"Widget": {"type": "object"}})
        assert ts_type({"$ref": "#/components/schemas/Widget"}, models) == "Widget"

    def test_unknown_ref_does_not_crash(self) -> None:
        models = ModelRegistry({})
        assert ts_type({"$ref": "#/components/schemas/Ghost"}, models) == "Ghost"

    def test_recursive_schema_terminates(self) -> None:
        node: dict[str, Any] = {"type": "object", "properties": {}}
        node["properties"]["self"] = node
        assert isinstance(ts_type(node, ModelRegistry({})), str)


class TestMinimalSchema:
    """End-to-end on a fake schema — proves `build_sdk` needs no app."""

    @pytest.fixture(scope="class")
    @classmethod
    def files(cls) -> dict[str, str]:
        return build_sdk(MINIMAL_SCHEMA, timestamp=None)

    def test_one_file_per_tag(self, files: dict[str, str]) -> None:
        assert set(files) == {"widgets.ts", "client.ts", "models.ts", "index.ts"}

    def test_method_signature(self, files: dict[str, str]) -> None:
        body = files["widgets.ts"]
        # No `By...` suffix: nothing else collides, so the path parameter is
        # left out of the name and adding one later won't rename the method.
        assert "export async function getWidgets(" in body
        assert "widgetId: string," in body
        assert "query?: { verbose?: boolean }," in body
        assert "options?: RequestOptions," in body
        assert "): Promise<Widget> {" in body

    def test_no_content_response_is_void(self, files: dict[str, str]) -> None:
        assert "): Promise<void> {" in files["widgets.ts"]

    def test_model_interface_emitted(self, files: dict[str, str]) -> None:
        models = files["models.ts"]
        assert "export interface Widget {" in models
        assert "id: string;" in models
        assert "size?: number;" in models
        assert "tags?: Array<string>;" in models

    def test_colliding_paths_get_distinct_names(self) -> None:
        """Two ops differing only by a path param must not clash."""
        schema: dict[str, Any] = {
            "paths": {
                "/api/v1/things": {"get": {"tags": ["t"], "summary": "list", "responses": {}}},
                "/api/v1/things/{thing_id}": {
                    "get": {
                        "tags": ["t"],
                        "summary": "one",
                        "parameters": [
                            {
                                "name": "thing_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {},
                    }
                },
            }
        }
        names = exported_functions(build_sdk(schema, timestamp=None)["t.ts"])
        assert len(names) == len(set(names)) == 2

    def test_untagged_operation_lands_in_default(self) -> None:
        schema: dict[str, Any] = {
            "paths": {"/api/v1/x": {"get": {"summary": "x", "responses": {}}}}
        }
        files = build_sdk(schema, timestamp=None)
        assert "default.ts" in files
        assert exported_functions(files["default.ts"]) == ["getX"]

    def test_empty_schema_still_produces_runtime(self) -> None:
        files = build_sdk({"paths": {}}, timestamp=None)
        assert set(files) == {"client.ts", "models.ts", "index.ts"}
        assert "export {};" in files["models.ts"]

    def test_schema_file_input(self, tmp_path: Path) -> None:
        """`--schema` bypasses the app entirely."""
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps(MINIMAL_SCHEMA), encoding="utf-8")
        out = tmp_path / "api"
        assert main(["--schema", str(schema_path), "--out", str(out), "--no-timestamp"]) == 0
        assert (out / "widgets.ts").exists()


class TestMultipartGap:
    """The documented BE-API-008 follow-up is honestly reflected in output."""

    def test_multipart_ops_are_flagged_not_dropped(
        self, sdk: dict[str, str], schema: dict[str, Any]
    ) -> None:
        multipart_paths = [
            (path, method)
            for path, methods in (schema.get("paths") or {}).items()
            for method, info in methods.items()
            if method.lower() in HTTP_METHODS
            and "multipart/form-data" in ((info.get("requestBody") or {}).get("content") or {})
        ]
        if not multipart_paths:
            pytest.skip("no multipart endpoints in the current app")

        combined = "\n".join(tag_modules(sdk).values())
        for path, method in multipart_paths:
            assert f"`{method.upper()} {path}`" in combined
        assert "multipart body not yet generated (BE-API-008)" in combined

    def test_json_bodies_are_typed(self, operations: list[Any]) -> None:
        typed = [op for op in operations if op.body_type is not None]
        assert typed, "expected at least one JSON body operation"
        assert all(op.body_type != "any" for op in typed)


class TestDocumentation:
    """AC-11 + the ticket's own follow-up note."""

    def test_readme_documents_the_command(self) -> None:
        readme = (gen_ts_sdk.ROOT / "README.md").read_text(encoding="utf-8")
        assert "make ts-sdk" in readme
        assert "gen_ts_sdk" in readme

    def test_makefile_has_target(self) -> None:
        makefile = (gen_ts_sdk.ROOT / "Makefile").read_text(encoding="utf-8")
        assert "ts-sdk:" in makefile

    def test_ticket_records_multipart_followup(self) -> None:
        ticket = (gen_ts_sdk.ROOT / "docs" / "be-issues" / "BE-API-007.md").read_text(
            encoding="utf-8"
        )
        assert "BE-API-008" in ticket
        assert "multipart" in ticket


class TestCheckedInOutputIsFresh:
    """The committed SDK matches what the generator produces today."""

    def test_frontend_api_up_to_date(self, sdk: dict[str, str]) -> None:
        out = gen_ts_sdk.DEFAULT_OUTPUT
        if not out.exists():
            pytest.skip("frontend/src/api not generated in this checkout")
        for name, content in sdk.items():
            target = out / name
            assert target.exists(), f"{name} missing — run `make ts-sdk`"
            assert strip_timestamp(target.read_text(encoding="utf-8")) == strip_timestamp(
                content
            ), f"{name} is stale — run `make ts-sdk`"
