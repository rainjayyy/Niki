#!/usr/bin/env python
"""Calibrate the LLM judge against a set of human-labeled cases.

The judge is only useful if it agrees with humans on what "good" and
"bad" responses look like. This script:

1. Loads a calibration set (cases with `human_score` 1-5)
2. Runs each case through the LLM judge
3. Binarizes both human and judge scores at threshold 4 (pass/fail)
4. Computes Cohen-kappa and per-case agreement
5. Outputs a calibration report

Usage:
    # Use the bundled calibration set (20 starter cases)
    python scripts/calibrate_judge.py

    # Use a custom set
    python scripts/calibrate_judge.py --calibration data/eval/my_labels.json

    # Save the report
    python scripts/calibrate_judge.py --report calibration_report.json

Exit codes:
    0 - kappa >= 0.7 (substantial agreement, judge is calibrated)
    1 - kappa < 0.7 (judge is NOT reliable; re-tune the prompt)
    2 - other error (e.g. invalid input)

What the human-label file looks like:
    [
        {
            "id": "cal_001",
            "input": "...",
            "response": "agent output to judge",
            "human_score": 4,
            "human_rationale": "covers the key points, no fear words",
            "age_group": "6-8"
        },
        ...
    ]

The `human_score` is your ground truth. The judge will score 1-5 too.
Cohen-kappa is computed on the binarized versions (>= 4 = pass).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# Make `niki` importable when run as `python scripts/...`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from niki.eval.judge import CalibrationReport, LLMJudge  # noqa: E402
from niki.infrastructure.llm.minimax import MinimaxClient  # noqa: E402

DEFAULT_CALIBRATION = ROOT / "data" / "eval" / "calibration_set.json"
PASS_THRESHOLD = 4  # >= 4 = pass; < 4 = fail
KAPPA_THRESHOLD = 0.7  # Cohen-kappa floor for "calibrated"


def _to_judge_client() -> Any:
    """Build a real judge client. Falls back to DoubaoClient if available,
    else minimax. Both must have a real API key in env.
    """
    if os.environ.get("DOUBAO_API_KEY") and not os.environ.get("DOUBAO_API_KEY", "").startswith(
        "your-"
    ):
        from niki.infrastructure.llm.doubao import DoubaoClient

        return DoubaoClient(
            api_key=os.environ["DOUBAO_API_KEY"],
            base_url=os.environ.get("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            model=os.environ.get("DOUBAO_MODEL", "doubao-lite-32k"),
        )
    return MinimaxClient(
        api_key=os.environ["MINIMAX_API_KEY"],
        base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"),
        model=os.environ.get("MINIMAX_MODEL", "MiniMax-M3"),
    )


async def _calibrate(cases: list[dict], client: Any) -> CalibrationReport:
    case_ids: list[str] = []
    judge = LLMJudge(model=client, swap_position_runs=False)
    judge_results: list = []
    human_scores: list[int] = []

    for i, case in enumerate(cases, start=1):
        result = await judge.score(
            case=case,
            response_text=case.get("response", ""),
            child_age=case.get("age_group", "6-8"),
        )
        case_id = case.get("id", f"cal_{i:03d}")
        case_ids.append(case_id)
        human = int(case["human_score"])
        judge_results.append(result)
        human_scores.append(human)
        print(
            f"  [{i:2d}/{len(cases)}] {case_id}: human={human} judge={result.score} {chr(0x2713) if abs(result.score - human) <= 1 else chr(0x2717)}"
        )

    return judge.compare_to_human(judge_results, human_scores, case_ids=case_ids)


def _load_calibration(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _print_report(report: CalibrationReport, n: int) -> int:
    print("\n" + "=" * 60)
    print("Judge Calibration Report")
    print("=" * 60)
    print(f"  Calibration set:        {n} cases")
    print(f"  Mean human score:       {report.mean_human_score:.2f} / 5")
    print(f"  Mean judge score:       {report.mean_judge_score:.2f} / 5")
    print(f"  Cohen-kappa:            {report.cohen_kappa:.3f}")
    print(f"  Calibration threshold:  kappa >= {KAPPA_THRESHOLD:.2f}")
    if report.is_calibrated:
        print("  Status:                 PASS - judge is reliable")
    else:
        print("  Status:                 FAIL - re-tune the judge prompt")
    print("\nPer-case agreement:")
    print(f"  {chr(0x2713)} = within 1 point of human; {chr(0x2717)} = > 1 point off")

    for case_id, judge_s, human_s in report.per_case_diff:
        delta = judge_s - human_s
        sign = "+" if delta > 0 else ""
        mark = chr(0x2713) if abs(delta) <= 1 else chr(0x2717)
        print(f"    {mark} {case_id:25s}  human={human_s}  judge={judge_s}  ({sign}{delta})")
    print("=" * 60)
    return 0 if report.is_calibrated else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--calibration",
        default=str(DEFAULT_CALIBRATION),
        help="Path to calibration JSON with human labels.",
    )
    parser.add_argument("--report", default=None, help="Save the calibration report to this path.")
    args = parser.parse_args()

    cal_path = Path(args.calibration)
    if not cal_path.exists():
        print(f"ERROR: calibration file not found: {cal_path}", file=sys.stderr)
        sys.exit(2)

    cases = _load_calibration(cal_path)
    # The JSON may be a bare list OR a {"cases": [...]} envelope.
    if isinstance(cases, dict) and "cases" in cases:
        cases = cases["cases"]
    if not cases:
        print("ERROR: calibration file is empty", file=sys.stderr)
        sys.exit(2)
    print(f"Loaded {len(cases)} calibration cases from {cal_path}")
    print("Running judge on each case...\n")

    client = _to_judge_client()
    report = asyncio.run(_calibrate(cases, client))
    exit_code = _print_report(report, len(cases))

    if args.report:
        Path(args.report).write_text(
            json.dumps(
                {
                    "n_cases": report.n_cases,
                    "cohen_kappa": report.cohen_kappa,
                    "is_calibrated": report.is_calibrated,
                    "mean_human_score": report.mean_human_score,
                    "mean_judge_score": report.mean_judge_score,
                    "per_case_diff": [
                        {"id": cid, "human": h, "judge": j} for cid, j, h in report.per_case_diff
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nReport written to {args.report}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
