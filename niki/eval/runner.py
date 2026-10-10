"""End-to-end eval runner — golden dataset → agent → dimensions → report.

The runner is the glue that ``scripts/run_eval.py`` calls. It is
intentionally minimal: load the golden dataset, drive a real or stubbed
agent over each case, compute the three dimensions, and aggregate
into a :class:`DimensionReport`.

The runner is the **only** place the eval layer talks to Niki agents.
Keeping this isolated means the rest of the layer (judge, dimensions)
stays Niki-independent and is unit-testable without importing the
orchestrator.

Usage::

    from niki.eval.runner import run_golden_dataset

    report = await run_golden_dataset(
        cases=load_golden_cases("data/eval/golden_dataset.json"),
        child_age=7,
        judge=LLMJudge(),
        run_agent=my_agent_function,  # async (case) -> response_text
    )
    print(report.pass_rates)
    # {"response_quality": 0.92, "hard_constraints": 1.0, "safety": 1.0}
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog

from niki.eval.dimensions import (
    DimensionReport,
    aggregate_dimensions,
    score_dimensions,
)
from niki.eval.judge import LLMJudge

logger = structlog.get_logger(__name__)


# A function that drives an agent over one case and returns the
# text the agent produced. The runner doesn't care which agent
# implementation is plugged in — the contract is just (case) -> str.
AgentRunner = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class CaseResult:
    """Per-case eval result: case_id + per-dimension scores."""

    case_id: str
    response_text: str
    per_dimension: tuple  # tuple[DimensionScore, ...] — frozen for hashing


def load_golden_cases(path: str | Path) -> list[dict[str, Any]]:
    """Load the golden dataset JSON.

    The dataset is a list of case dicts; we keep them as raw dicts
    so callers can add new fields without changing the eval layer.

    Args:
        path: Path to ``data/eval/golden_dataset.json`` (or
            ``golden_dataset_stub.json`` for the smoke run).

    Returns:
        List of case dicts.

    Raises:
        FileNotFoundError: If the dataset file does not exist.
        ValueError: If the file isn't a list of case dicts.
    """
    import json

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"golden dataset not found: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(
            f"golden dataset must be a JSON list of case dicts, got {type(raw).__name__}"
        )
    for i, case in enumerate(raw):
        if not isinstance(case, dict):
            raise ValueError(f"case #{i} is not a dict: {type(case).__name__}")
    return raw


async def run_golden_dataset(
    cases: list[dict[str, Any]],
    *,
    child_age: int,
    judge: LLMJudge | None,
    run_agent: AgentRunner,
    on_progress: Callable[[int, int, CaseResult], None] | None = None,
) -> DimensionReport:
    """Run the full golden dataset and aggregate the 3 dimensions.

    Args:
        cases: Golden dataset (output of :func:`load_golden_cases`).
        child_age: Age in years, used by the agent runner and by the
            judge prompt.
        judge: LLM-as-judge instance. ``None`` means the
            ``response_quality`` dimension will fail-closed (this is
            useful for hard-asserts-only smoke runs).
        run_agent: Async function that drives an agent over one
            case and returns the response text. The runner doesn't
            know which agent; the test or script plugs in the
            appropriate one.
        on_progress: Optional callback ``(done, total, case_result)``
            for progress reporting. Useful for long runs that need
            a tqdm-like UI without depending on tqdm.

    Returns:
        :class:`DimensionReport` with per-dimension pass rates.
    """
    case_results: list[CaseResult] = []
    for idx, case in enumerate(cases):
        case_id = str(case.get("id", f"case_{idx:03d}"))
        try:
            response = await run_agent(case)
        except Exception as exc:  # noqa: BLE001 — surface as response
            logger.warning(
                "runner.agent_error",
                case_id=case_id,
                error=str(exc),
            )
            response = ""
        scores = await score_dimensions(case, response, child_age, judge)
        result = CaseResult(
            case_id=case_id,
            response_text=response,
            per_dimension=tuple(scores),
        )
        case_results.append(result)
        if on_progress is not None:
            try:
                on_progress(idx + 1, len(cases), result)
            except Exception:  # noqa: BLE001
                logger.warning("runner.progress_callback_failed", case_id=case_id)
    report = aggregate_dimensions([r.per_dimension for r in case_results])
    logger.info(
        "runner.complete",
        n_cases=report.n_cases,
        pass_rates=report.pass_rates,
        overall_passed=report.overall_passed,
    )
    return report


__all__ = [
    "AgentRunner",
    "CaseResult",
    "load_golden_cases",
    "run_golden_dataset",
]
