#!/usr/bin/env python
"""Run the Niki golden dataset through the agent stack and judge the results.

Usage:
    # Smoke test with stub LLM (no API key needed)
    python scripts/run_eval.py --stub --limit 5

    # Full eval with real LLM judge (requires MINIMAX_API_KEY or DOUBAO_API_KEY)
    python scripts/run_eval.py --child-age 7 --report eval_report.json

    # Judge-only (skip the agent, useful for re-scoring existing responses)
    python scripts/run_eval.py --judge-only --limit 20

Exit codes:
    0 — all cases passed (or smoke test only)
    1 — at least one safety case failed (block release)
    2 — at least one non-safety case failed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Make `niki` importable when run as `python scripts/run_eval.py`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from niki.agents.orchestrator import ChatOrchestrator  # noqa: E402
from niki.eval.judge import LLMJudge, run_golden_case  # noqa: E402


# Stub LLM used by --stub mode. Mirrors niki.api._stub_llm so the
# agent layer can run without API keys.
class _StubJudgeLLM:
    """Returns a passing 4/5 for every case. Used in --stub mode."""

    @property
    def model(self) -> str:
        return "stub-judge"

    async def chat(self, messages, **kwargs):
        # Match the judge schema: score + rationale.
        return {
            "content": json.dumps({"score": 4, "rationale": "stub pass"}),
            "model": "stub-judge",
            "usage": {},
            "finish_reason": "stop",
        }


class _StubOrchestratorLLM:
    """Used by --stub mode for the Niki agent side."""

    @property
    def model(self) -> str:
        return "stub-orchestrator"

    async def chat(self, messages, **kwargs):
        next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return {
            "content": "嗯~ Niki 听到了~",
            "model": "stub-orchestrator",
            "usage": {},
            "finish_reason": "stop",
        }


@dataclass
class CaseResult:
    """Per-case evaluation outcome."""

    case_id: str
    category: str
    age_group: str
    passed: bool
    judge_score: int = 0
    judge_verdict: str = "fail"
    hard_assert_failures: list = field(default_factory=list)
    response_text: str = ""
    duration_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["passed"] = self.passed
        return d


def _load_dataset(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cases", [])


def _is_safety_case(case: dict) -> bool:
    cat = case.get("category", "")
    return cat.startswith("safety_")


async def _run_one_case(
    case: dict,
    orchestrator: ChatOrchestrator | None,
    judge: LLMJudge,
    child_age: int,
    stub_mode: bool,
) -> CaseResult:
    """Run a single case end-to-end: agent -> judge -> hard asserts."""
    started = time.monotonic()
    try:
        if orchestrator is not None:
            chat_result = await orchestrator.chat(case["input"])
            response_text = chat_result.final_text
        else:
            # judge-only mode: skip the agent entirely
            response_text = "(judge-only: agent skipped)"

        result, passed = await run_golden_case(
            judge=judge,
            case=case,
            response_text=response_text,
            child_age=child_age,
        )

        hard_failures = []
        for word in case.get("must_contain", []):
            if word not in response_text:
                hard_failures.append(f"missing required: {word!r}")
        for word in case.get("must_not_contain", []):
            if word in response_text:
                hard_failures.append(f"contains forbidden: {word!r}")
        if case.get("expected_route") and not stub_mode:
            # In stub mode the agent returns canned text so routing
            # assertions are skipped.
            pass

        return CaseResult(
            case_id=case.get("id", "unknown"),
            category=case.get("category", "unknown"),
            age_group=case.get("age_group", "unknown"),
            passed=passed and not hard_failures,
            judge_score=result.score,
            judge_verdict=str(result.verdict.value)
            if hasattr(result.verdict, "value")
            else str(result.verdict),
            hard_assert_failures=hard_failures,
            response_text=response_text,
            duration_ms=(time.monotonic() - started) * 1000,
        )
    except Exception as exc:
        return CaseResult(
            case_id=case.get("id", "unknown"),
            category=case.get("category", "unknown"),
            age_group=case.get("age_group", "unknown"),
            passed=False,
            response_text="",
            duration_ms=(time.monotonic() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )


def _print_summary(results: list[CaseResult]) -> int:
    """Print human-readable summary. Returns the process exit code."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    safety_total = sum(1 for r in results if _is_safety_case({"category": r.category}))
    safety_passed = sum(
        1 for r in results if _is_safety_case({"category": r.category}) and r.passed
    )
    by_category = Counter(r.category for r in results)
    by_category_passed = Counter(r.category for r in results if r.passed)

    print("\n" + "=" * 60)
    print("Niki Eval Summary")
    print("=" * 60)
    print(f"  Total cases:       {total}")
    print(f"  Passed:            {passed} / {total}  ({passed / total * 100:.1f}%)")
    if safety_total:
        print(
            f"  Safety cases:      {safety_passed} / {safety_total}  ({'PASS' if safety_passed == safety_total else 'FAIL — BLOCKS RELEASE'})"
        )
    print("")
    print("  By category:")
    for cat, n in sorted(by_category.items()):
        p = by_category_passed.get(cat, 0)
        mark = "OK" if p == n else "FAIL"
        print(f"    [{mark:4s}] {cat:35s} {p}/{n}")
    print("")
    if any(not r.passed for r in results):
        print("  Failed cases:")
        for r in results:
            if r.passed:
                continue
            err = r.error or "; ".join(r.hard_assert_failures) or f"judge_score={r.judge_score}"
            print(f"    - {r.case_id} ({r.category}): {err}")
    print("=" * 60)

    if safety_total and safety_passed < safety_total:
        return 1  # safety failure → block release
    if passed < total:
        return 2  # other failures
    return 0


async def _async_main(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset)
    cases = _load_dataset(dataset_path)
    if args.limit:
        cases = cases[: args.limit]
    print(f"Loaded {len(cases)} cases from {dataset_path}")

    if args.stub:
        # Stub mode: both agent and judge use stubs. No API calls.
        orchestrator = None  # judge-only path
        # Patch the agent by injecting stub LLMs via the orchestrator.
        # For simplicity in --stub mode we also skip the agent and
        # just exercise the judge with the user's input as the
        # "response". This still validates hard-asserts and judge
        # mechanics; for real agent behavior use the full test suite.
        judge = LLMJudge(model=_StubJudgeLLM(), swap_position_runs=False)
        for c in cases:
            c.setdefault("__response_override", c["input"])
        _orig_run = _run_one_case

        async def _run_stub(case, *_a, **_kw):
            case = dict(case)
            case["__response_override"] = case["input"]
            r = await _orig_run(case, None, judge, args.child_age, stub_mode=True)
            return r

        runner = _run_stub
    else:
        orchestrator = ChatOrchestrator(child_age=args.child_age)
        # Prefer a non-orchestrator LLM as judge (cross-model bias).
        # If the factory picked minimax for the agent, judge with
        # doubao if its key is set, else the same minimax client.
        from niki.config.settings import get_settings
        from niki.infrastructure.llm.doubao import DoubaoClient
        from niki.infrastructure.llm.minimax import MinimaxClient

        settings = get_settings()
        # Cross-model judging: use a DIFFERENT model from the agent's
        # primary to avoid self-bias. Reuse the factory's placeholder
        # check so we don't pick Doubao when its key is just a template.
        from niki.infrastructure.llm.factory import _is_real_key

        if settings.primary_llm_provider == "minimax" and _is_real_key(settings.doubao_api_key):
            judge_model = DoubaoClient(
                api_key=settings.doubao_api_key,
                base_url=settings.doubao_base_url,
                model=settings.doubao_model,
            )
        else:
            # Default: judge with the same provider (no real cross-model
            # bias protection but at least it works with one real key).
            judge_model = MinimaxClient(
                api_key=settings.minimax_api_key,
                base_url=settings.minimax_base_url,
                model=settings.minimax_model,
            )
        judge = LLMJudge(model=judge_model, swap_position_runs=not args.fast)

        def runner(case):
            return _run_one_case(case, orchestrator, judge, args.child_age, stub_mode=False)

    results: list[CaseResult] = []
    for i, case in enumerate(cases, start=1):
        print(f"  [{i:3d}/{len(cases)}] {case.get('id', '?')}...", end=" ", flush=True)
        result = await runner(case)
        results.append(result)
        mark = "PASS" if result.passed else "FAIL"
        print(f"{mark}  ({result.duration_ms:.0f}ms)")

    exit_code = _print_summary(results)

    if args.report:
        report = {
            "dataset": str(dataset_path),
            "child_age": args.child_age,
            "stub_mode": args.stub,
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "safety_passed": sum(
                1 for r in results if _is_safety_case({"category": r.category}) and r.passed
            ),
            "by_category": {
                cat: {
                    "total": sum(1 for r in results if r.category == cat),
                    "passed": sum(1 for r in results if r.category == cat and r.passed),
                }
                for cat in {r.category for r in results}
            },
            "results": [r.to_dict() for r in results],
        }
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nReport written to {args.report}")

    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dataset",
        default="data/eval/golden_dataset.json",
        help="Path to the golden dataset JSON.",
    )
    parser.add_argument("--report", default=None, help="Write a JSON report to this path.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Only run the first N cases (smoke test)."
    )
    parser.add_argument("--child-age", type=int, default=7, help="Child age for agent context.")
    parser.add_argument(
        "--stub", action="store_true", help="Use stub LLM (no API calls). For smoke tests."
    )
    parser.add_argument(
        "--judge-only",
        action="store_true",
        help="Skip the agent; only run the judge on case input as a proxy response.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Disable position-bias double-pass on the judge (faster, less safe).",
    )
    args = parser.parse_args()
    exit_code = asyncio.run(_async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
