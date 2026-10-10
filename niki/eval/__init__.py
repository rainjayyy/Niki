"""Niki eval module — golden dataset + LLM-as-judge + 3 named dimensions.

Public API:
    - :class:`niki.eval.judge.LLMJudge` — LLM-as-judge with 4 bias controls
    - :func:`niki.eval.dimensions.score_dimensions` — score one case across
      the 3 named dimensions (response_quality / hard_constraints / safety)
    - :func:`niki.eval.dimensions.aggregate_dimensions` — per-dimension
      pass-rate report over many cases
    - :func:`niki.eval.runner.run_golden_dataset` — end-to-end smoke runner
      that loads ``data/eval/golden_dataset.json``, runs the agent
      stub, and aggregates the 3 dimensions
"""

from niki.eval.dimensions import (
    Dimension,
    DimensionReport,
    DimensionScore,
    aggregate_dimensions,
    hard_constraints,
    response_quality,
    safety,
    score_dimensions,
)
from niki.eval.judge import (
    JudgeResult,
    JudgeVerdict,
    LLMJudge,
    run_golden_case,
)

__all__ = [
    # judge
    "JudgeResult",
    "JudgeVerdict",
    "LLMJudge",
    "run_golden_case",
    # dimensions
    "Dimension",
    "DimensionScore",
    "DimensionReport",
    "score_dimensions",
    "aggregate_dimensions",
    "response_quality",
    "hard_constraints",
    "safety",
]
