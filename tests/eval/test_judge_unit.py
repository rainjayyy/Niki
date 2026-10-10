"""Unit tests for niki.eval.judge.LLMJudge.

These tests are intentionally Niki-independent — they mock the LLM
client (DoubaoClient) and verify the judge's deterministic logic:

- JSON parsing of judge output (valid / invalid / boundary scores)
- Length normalization
- Score-to-verdict mapping
- Cohen-kappa computation
- Position-bias detection (run twice, disagree → flag)
- Malformed-output handling
- compare_to_human calibration report

They can be run without any LLM API keys or Niki infrastructure.
"""

from __future__ import annotations

import pytest

from niki.eval.judge import (
    DEFAULT_MAX_LENGTH_FOR_JUDGE,
    JudgeResult,
    JudgeVerdict,
    LLMJudge,
    run_golden_case,
)

# ---------------------------------------------------------------------------
# Mock LLM client (no API calls)
# ---------------------------------------------------------------------------


class FallbackLLM:
    """Configurable stub that mimics DoubaoClient.chat().

    Usage:
        stub = FallbackLLM(["score": 4, ...], "score": 5, ...])
        # Each .chat() call returns the next canned response, cycling.
    """

    def __init__(self, responses: list | None = None, default: str = "") -> None:
        self._responses = list(responses or [])
        self._default = default
        self.calls: list = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        content = self._responses.pop(0) if self._responses else self._default
        return {"content": content, "model": "stub"}

    @property
    def model(self) -> str:
        return "stub-doubao"


def _make_judge(*stub_responses, swap_position_runs: bool = True) -> tuple:
    """Create a judge with a stub LLM that returns the given responses in order."""
    stub = FallbackLLM(list(stub_responses))
    judge = LLMJudge(model=stub, swap_position_runs=swap_position_runs)
    return judge, stub


def _valid_score_json(score: int = 4, criteria_count: int = 4) -> str:
    """Build a well-formed judge-output JSON string."""
    import json

    return json.dumps(
        {
            "score": score,
            "rationale": "test rationale",
            "criteria_met": [True] * criteria_count,
        }
    )


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


class TestParseJudgeOutput:
    """Boundary cases for the strict JSON schema validator."""

    def test_valid_json_with_code_fences(self):
        raw = """```json
        {"score": 4, "rationale": "ok", "criteria_met": [true]}
        ```"""
        parsed = LLMJudge._parse_judge_output(raw)
        assert parsed["score"] == 4
        assert parsed["criteria_met"] == [True]

    def test_valid_json_without_fences(self):
        parsed = LLMJudge._parse_judge_output(_valid_score_json(score=5))
        assert parsed["score"] == 5

    def test_rejects_non_json(self):
        with pytest.raises(ValueError, match="valid JSON"):
            LLMJudge._parse_judge_output("not json at all")

    def test_rejects_json_object_outside_1_to_5(self):
        with pytest.raises(ValueError, match="not in 1-5"):
            LLMJudge._parse_judge_output('{"score": 0, "rationale": "x"}')
        with pytest.raises(ValueError, match="not in 1-5"):
            LLMJudge._parse_judge_output('{"score": 6, "rationale": "x"}')
        with pytest.raises(ValueError, match="not in 1-5"):
            LLMJudge._parse_judge_output('{"score": "4", "rationale": "x"}')

    def test_rejects_json_array(self):
        with pytest.raises(ValueError, match="not a JSON object"):
            LLMJudge._parse_judge_output("[1, 2, 3]")

    def test_rejects_missing_score_field(self):
        with pytest.raises(ValueError, match="missing score"):
            LLMJudge._parse_judge_output('{"rationale": "x", "criteria_met": []}')


# ---------------------------------------------------------------------------
# Length normalization
# ---------------------------------------------------------------------------


class TestNormalize:
    """Verify the 200-char cap + whitespace collapse."""

    def test_short_text_passes_through(self):
        assert LLMJudge._normalize("hi", 200) == "hi"

    def test_collapses_whitespace(self):
        assert LLMJudge._normalize("a\n\n  b\t  c  ", 200) == "a b c"

    def test_truncates_long_text_with_ellipsis(self):
        long = "x" * 500
        out = LLMJudge._normalize(long, 200)
        assert len(out) == 200
        assert out.endswith("...")

    def test_empty_string(self):
        assert LLMJudge._normalize("", 200) == ""
        assert LLMJudge._normalize(None, 200) == ""


# ---------------------------------------------------------------------------
# Score to verdict mapping
# ---------------------------------------------------------------------------


class TestScoreToVerdict:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (1, JudgeVerdict.FAIL),
            (2, JudgeVerdict.FAIL),
            (3, JudgeVerdict.WEAK),
            (4, JudgeVerdict.PASS),
            (5, JudgeVerdict.STRONG),
        ],
    )
    def test_score_to_verdict_boundaries(self, score, expected):
        assert LLMJudge._score_to_verdict(score) == expected


# ---------------------------------------------------------------------------
# Cohen kappa
# ---------------------------------------------------------------------------


class TestCohenKappa:
    def test_perfect_agreement(self):
        assert LLMJudge._cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0

    def test_perfect_disagreement_random(self):
        # Anti-correlated binary → expected kappa close to -1
        k = LLMJudge._cohen_kappa([1, 1, 0, 0], [0, 0, 1, 1])
        assert k < 0

    def test_chance_agreement(self):
        # Both lists uniform 50/50 → kappa should be ~0
        k = LLMJudge._cohen_kappa([1, 0, 1, 0, 1, 0], [1, 0, 0, 1, 1, 0])
        assert -0.5 < k < 0.5

    def test_empty_input(self):
        assert LLMJudge._cohen_kappa([], []) == 0.0

    def test_mismatched_length_raises(self):
        with pytest.raises(ValueError, match="same length"):
            LLMJudge._cohen_kappa([1, 0], [1, 0, 1])


# ---------------------------------------------------------------------------
# Async score() with mocked LLM
# ---------------------------------------------------------------------------


class TestScoreHappyPath:
    async def test_score_returns_pass_verdict(self):
        judge, _ = _make_judge(_valid_score_json(score=4), _valid_score_json(score=4))
        case = {
            "id": "test_001",
            "input": "你好",
            "llm_judge_criteria": "good",
            "min_llm_judge_score": 4,
        }
        result = await judge.score(case, "你好呀~", child_age=7)
        assert result.score == 4
        assert result.verdict == JudgeVerdict.PASS
        assert not result.malformed
        assert not result.position_bias_detected

    async def test_score_handles_malformed_output(self):
        judge, _ = _make_judge("not json", "still not json", swap_position_runs=False)
        case = {"id": "test_002", "input": "x", "llm_judge_criteria": "x"}
        result = await judge.score(case, "response", child_age=7)
        assert result.malformed is True
        assert result.score == 0
        assert result.verdict == JudgeVerdict.FAIL

    async def test_score_with_swap_position_runs_disabled(self):
        judge, stub = _make_judge(_valid_score_json(score=4), swap_position_runs=False)
        case = {"id": "t3", "input": "x", "llm_judge_criteria": "x"}
        await judge.score(case, "response", child_age=7)
        # Without swap, only 1 LLM call is made
        assert len(stub.calls) == 1

    async def test_score_with_swap_position_runs_enabled(self):
        judge, stub = _make_judge(_valid_score_json(score=4), _valid_score_json(score=4))
        case = {"id": "t4", "input": "x", "llm_judge_criteria": "x"}
        await judge.score(case, "response", child_age=7)
        # With swap, 2 LLM calls are made
        assert len(stub.calls) == 2


class TestScorePositionBias:
    """When two passes disagree by ≥2, the lower wins and bias is flagged."""

    async def test_high_disagreement_flags_bias(self):
        # First pass scores 5, second pass scores 2 → delta=3, biased
        judge, _ = _make_judge(_valid_score_json(score=5), _valid_score_json(score=2))
        case = {"id": "bias_001", "input": "x", "llm_judge_criteria": "x"}
        result = await judge.score(case, "response", child_age=7)
        assert result.position_bias_detected is True
        # Conservative: take the lower score
        assert result.score == 2

    async def test_small_disagreement_not_flagged(self):
        # First pass 4, second pass 5 → delta=1, not biased
        judge, _ = _make_judge(_valid_score_json(score=4), _valid_score_json(score=5))
        case = {"id": "no_bias", "input": "x", "llm_judge_criteria": "x"}
        result = await judge.score(case, "response", child_age=7)
        assert result.position_bias_detected is False
        # No flag → first score wins (no conservative override)
        assert result.score == 4


# ---------------------------------------------------------------------------
# run_golden_case convenience wrapper
# ---------------------------------------------------------------------------


class TestRunGoldenCase:
    """Verify the helper combines judge score with hard-assert checks."""

    async def test_passes_when_judge_and_hard_asserts_agree(self):
        judge, _ = _make_judge(_valid_score_json(score=4), _valid_score_json(score=4))
        case = {
            "id": "ok_001",
            "input": "你好",
            "must_contain": ["你"],
            "must_not_contain": ["脏话"],
            "min_llm_judge_score": 4,
        }
        result, passed = await run_golden_case(judge, case, "你好呀~", child_age=7)
        assert passed is True
        assert result.score == 4

    async def test_fails_when_must_contain_missing(self):
        judge, _ = _make_judge(_valid_score_json(score=5), _valid_score_json(score=5))
        case = {
            "id": "miss_001",
            "input": "x",
            "must_contain": ["必需的关键词"],
            "min_llm_judge_score": 4,
        }
        result, passed = await run_golden_case(judge, case, "response", child_age=7)
        assert passed is False  # hard assert failed even though judge=5
        assert result.score == 5  # judge still recorded the score

    async def test_fails_when_must_not_contain_present(self):
        judge, _ = _make_judge(_valid_score_json(score=5), _valid_score_json(score=5))
        case = {
            "id": "leak_001",
            "input": "x",
            "must_not_contain": ["禁止词"],
            "min_llm_judge_score": 4,
        }
        result, passed = await run_golden_case(judge, case, "包含禁止词", child_age=7)
        assert passed is False

    async def test_uses_default_min_score_4_when_unspecified(self):
        judge, _ = _make_judge(_valid_score_json(score=3), _valid_score_json(score=3))
        case = {"id": "default_001", "input": "x", "llm_judge_criteria": "x"}
        # Score 3 is below the default threshold 4 → fail
        result, passed = await run_golden_case(judge, case, "response", child_age=7)
        assert passed is False
        assert result.score == 3


# ---------------------------------------------------------------------------
# Calibration report
# ---------------------------------------------------------------------------


class TestCalibration:
    def test_perfect_calibration_yields_kappa_one(self):
        judge, _ = _make_judge(_valid_score_json(score=4))
        # Two JudgeResults both scoring 4, two human scores both 4 → kappa=1
        r1 = JudgeResult(score=4, verdict=JudgeVerdict.PASS, rationale="ok", judge_model="stub")
        r2 = JudgeResult(score=5, verdict=JudgeVerdict.STRONG, rationale="ok", judge_model="stub")
        report = judge.compare_to_human([r1, r2], [4, 5])
        assert report.cohen_kappa == 1.0
        assert report.is_calibrated is True
        assert report.n_cases == 2

    def test_chance_calibration_yields_low_kappa(self):
        judge, _ = _make_judge(_valid_score_json(score=4))
        r1 = JudgeResult(score=4, verdict=JudgeVerdict.PASS, rationale="ok", judge_model="stub")
        r2 = JudgeResult(score=2, verdict=JudgeVerdict.FAIL, rationale="ok", judge_model="stub")
        # Judge says pass/fail, human says opposite → low kappa
        report = judge.compare_to_human([r1, r2], [2, 5])
        assert report.cohen_kappa < 0.5
        assert report.is_calibrated is False

    def test_length_mismatch_raises(self):
        judge, _ = _make_judge(_valid_score_json(score=4))
        r1 = JudgeResult(score=4, verdict=JudgeVerdict.PASS, rationale="ok", judge_model="stub")
        with pytest.raises(ValueError, match="length mismatch"):
            judge.compare_to_human([r1], [4, 5])


# ---------------------------------------------------------------------------
# Independence: this test file should NOT import Niki internals
# ---------------------------------------------------------------------------


def test_no_niki_internal_imports():
    """If this test ever fails, you accidentally coupled the judge to Niki.

    Run it via:
        python -c "import tests.eval.test_judge_unit; tests.eval.test_judge_unit.test_no_niki_internal_imports()"
    """
    import ast
    import inspect
    from pathlib import Path

    this_file = Path(__file__)
    source = this_file.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = ("niki.agents", "niki.api", "niki.auth", "niki.services", "niki.db")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
        elif isinstance(node, ast.Import):
            mod = (node.names[0].name if node.names else "") or ""
        else:
            continue
            for f in forbidden:
                if mod.startswith(f):
                    pytest.fail(f"judge_unit tests must not import Niki internals: {mod}")

    # Also check the module we test against
    from niki.eval import judge as judge_module

    judge_source = inspect.getsource(judge_module)
    for f in forbidden:
        if f in judge_source:
            pytest.fail(f"niki.eval.judge must not import from {f} (keep it portable)")


# ---------------------------------------------------------------------------
# Regression: DEFAULT_MAX_LENGTH_FOR_JUDGE constant
# ---------------------------------------------------------------------------


def test_default_max_length_is_200():
    """Lock down the bias-control knob. Changing this requires a code review."""
    assert DEFAULT_MAX_LENGTH_FOR_JUDGE == 200
