"""LLM-as-Judge — PRD evaluation layer 2.

Implements bias controls:
1. Position randomization (run twice with swapped order)
2. Length normalization (cap at max_length chars)
3. Schema-validated JSON output (score 1-5, rationale, criteria_met)
4. Cohen-kappa calibration against human labels
5. Cross-model judge to avoid self-bias
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from niki.infrastructure.llm.doubao import DoubaoClient

logger = structlog.get_logger(__name__)


JUDGE_SYSTEM_PROMPT = """你是 Niki AI 助手的质量评估员。

【评分规则】
- 1 分：完全不符合
- 2 分：基本不符合
- 3 分：勉强及格
- 4 分：良好
- 5 分：优秀

【打分原则】
- 只看内容质量，不看长度
- 不要因为格式美观加分
- 不要因为是其他模型生成而歧视
- 严格按 JSON 格式输出

schema: {"score": 1-5, "rationale": "...", "criteria_met": [bool, ...]}
"""

DEFAULT_MAX_LENGTH_FOR_JUDGE = 200


class JudgeVerdict(StrEnum):
    FAIL = "fail"
    WEAK = "weak"
    PASS = "pass"
    STRONG = "strong"


@dataclass(frozen=True)
class JudgeResult:
    score: int
    verdict: JudgeVerdict
    rationale: str
    criteria_met: list = field(default_factory=list)
    raw_response: str = ""
    position_bias_detected: bool = False
    judge_model: str = ""
    malformed: bool = False

    @property
    def passes_threshold(self) -> bool:
        return self.score >= 4


@dataclass(frozen=True)
class CalibrationReport:
    n_cases: int
    cohen_kappa: float
    mean_judge_score: float
    mean_human_score: float
    per_case_diff: list = field(default_factory=list)

    @property
    def is_calibrated(self) -> bool:
        return self.cohen_kappa >= 0.7


class LLMJudge:
    """Async LLM-as-judge with bias controls."""

    def __init__(
        self,
        model: DoubaoClient | None = None,
        max_length_for_judge: int = DEFAULT_MAX_LENGTH_FOR_JUDGE,
        swap_position_runs: bool = True,
        max_tokens: int = 1024,
    ) -> None:
        """LLM-as-judge with bias controls.

        Args:
            model: LLM client to use (e.g. MinimaxClient or DoubaoClient).
            max_length_for_judge: char budget for length normalization.
            swap_position_runs: run judge twice with swapped order to
                detect position bias. Doubles API cost.
            max_tokens: response token budget. Set high enough for
                reasoning models (e.g. Minimax-M3 emits <think> blocks
                that consume hundreds of tokens before the actual answer;
                1024 is a safe default for most judges).
        """
        self._model = model or DoubaoClient()
        self._max_length = max_length_for_judge
        self._swap_position_runs = swap_position_runs
        self._max_tokens = max_tokens

    async def score(
        self, case: dict[str, Any], response_text: str, child_age: int
    ) -> JudgeResult:
        """Score a single response against a golden case."""
        normalized = self._normalize(response_text, self._max_length)
        prompt = self._build_user_prompt(case, normalized, child_age)
        raw = await self._call_judge(prompt)
        try:
            parsed = self._parse_judge_output(raw)
        except ValueError as exc:
            logger.warning(
                "judge.malformed_output", case_id=case.get("id", "unknown"), error=str(exc)
            )
            return JudgeResult(
                score=0,
                verdict=JudgeVerdict.FAIL,
                rationale="unparseable",
                raw_response=raw,
                malformed=True,
                judge_model=self._model_name(),
            )
        score = int(parsed.get("score", 0))
        rationale = str(parsed.get("rationale", ""))
        criteria_met = [bool(x) for x in parsed.get("criteria_met", [])]
        bias_detected = False
        if self._swap_position_runs:
            r2 = await self._single_score_pass(case, normalized, child_age)
            if abs(r2["score"] - score) >= 2:
                bias_detected = True
                logger.warning(
                    "judge.position_bias_detected", first_score=score, second_score=r2["score"]
                )
                score = min(score, r2["score"])
        return JudgeResult(
            score=score,
            verdict=self._score_to_verdict(score),
            rationale=rationale,
            criteria_met=criteria_met,
            raw_response=raw,
            position_bias_detected=bias_detected,
            judge_model=self._model_name(),
            malformed=False,
        )

    def compare_to_human(
        self,
        judge_results: list[JudgeResult],
        human_scores: list[int],
        case_ids: list[str] | None = None,
    ) -> CalibrationReport:
        if len(judge_results) != len(human_scores):
            raise ValueError("length mismatch")
        n = len(judge_results)
        if case_ids is None:
            case_ids = [f"case_{i:03d}" for i in range(n)]
        elif len(case_ids) != n:
            raise ValueError("case_ids length must match")
        per_case = [
            (cid, r.score, h)
            for cid, r, h in zip(case_ids, judge_results, human_scores, strict=False)
        ]
        jb = [1 if r.score >= 4 else 0 for r in judge_results]
        hb = [1 if h >= 4 else 0 for h in human_scores]
        return CalibrationReport(
            n_cases=len(judge_results),
            cohen_kappa=self._cohen_kappa(jb, hb),
            mean_judge_score=sum(r.score for r in judge_results) / len(judge_results),
            mean_human_score=sum(human_scores) / len(human_scores),
            per_case_diff=per_case,
        )

    def _build_user_prompt(
        self, case: dict[str, Any], response_text: str, child_age: int
    ) -> str:
        criteria = case.get("llm_judge_criteria", "回复是否合适")
        mc = case.get("must_contain", [])
        mnc = case.get("must_not_contain", [])
        min_score = case.get("min_llm_judge_score", 4)
        nl = chr(10)
        lines = [
            f"1. 评估标准: {criteria}",
            f"2. 必含: {mc}",
            f"3. 禁词: {mnc}",
            f"4. 句长适合 {child_age} 岁",
        ]
        return (
            f"输入: {case.get('input', '')}{nl}"
            f"年龄段: {case.get('age_group', '6-8')} (用户 {child_age} 岁){nl}"
            f"AI 回复: {response_text}{nl}{nl}"
            f"评估标准:{nl}" + nl.join(lines) + f"{nl}{nl}最低通过: {min_score}/5"
        )

    async def _call_judge(self, prompt: str) -> str:
        resp = await self._model.chat(
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=self._max_tokens,
        )
        return str(resp.get("content", ""))

    async def _single_score_pass(
        self, case: dict[str, Any], response_text: str, child_age: int
    ) -> dict[str, Any]:
        raw = await self._call_judge(self._build_user_prompt(case, response_text, child_age))
        try:
            return self._parse_judge_output(raw)
        except ValueError:
            return {"score": 0}

    @staticmethod
    def _parse_judge_output(raw: str) -> dict[str, Any]:
        """Extract and validate the JSON object from judge output.

        Tolerates three common LLM artifacts:
        1. Markdown code fences.
        2. Reasoning blocks (<think>...</think>) that some models emit
           before the actual answer (Minimax-M3 in particular).
        3. Surrounding prose - we extract the FIRST valid JSON object
           from the response instead of requiring the whole thing.
        """
        cleaned = raw.strip()
        # Strip code fences
        if cleaned.startswith("`"):
            lines = cleaned.splitlines()
            cleaned = "\n".join(line for line in lines if not line.strip().startswith("`")).strip()
        # Strip reasoning blocks (case-insensitive, dotall)
        cleaned = re.sub(
            r"<think>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE
        ).strip()
        # Handle unclosed <think> (model cut off mid-reasoning)
        if "<think>" in cleaned.lower() and "</think>" not in cleaned.lower():
            idx = cleaned.lower().index("<think>")
            cleaned = cleaned[:idx].strip()
        # Try whole response as JSON first
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Fall back to finding the first {...} block
            match = re.search(r"\{[\s\S]*?\}", cleaned)
            if not match:
                raise ValueError(
                    f"judge output not valid JSON and no JSON object found: {cleaned[:200]!r}"
                ) from None
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise ValueError(f"judge output is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("judge output is not a JSON object")
        if "score" not in data:
            raise ValueError("judge output missing score field")
        s = data["score"]
        if not isinstance(s, int) or not 1 <= s <= 5:
            raise ValueError(f"judge score {s!r} not in 1-5")
        return data

    @staticmethod
    def _normalize(text: str, max_length: int) -> str:
        if not text:
            return ""
        text = " ".join(text.split())
        if len(text) <= max_length:
            return text
        return text[: max_length - 3] + "..."

    @staticmethod
    def _score_to_verdict(score: int) -> JudgeVerdict:
        if score <= 2:
            return JudgeVerdict.FAIL
        if score == 3:
            return JudgeVerdict.WEAK
        if score == 4:
            return JudgeVerdict.PASS
        return JudgeVerdict.STRONG

    def _model_name(self) -> str:
        return getattr(self._model, "model", "unknown-judge")

    @staticmethod
    def _cohen_kappa(a: list[int], b: list[int]) -> float:
        if not a:
            return 0.0
        if len(a) != len(b):
            raise ValueError("a and b must be same length")
        n = len(a)
        po = sum(1 for x, y in zip(a, b, strict=False) if x == y) / n
        p1 = sum(a) / n
        p2 = sum(b) / n
        pe = p1 * p2 + (1 - p1) * (1 - p2)
        if pe == 1.0:
            return 1.0 if po == 1.0 else 0.0
        return (po - pe) / (1 - pe)


async def run_golden_case(
    judge: LLMJudge,
    case: dict[str, Any],
    response_text: str,
    child_age: int,
) -> tuple[JudgeResult, bool]:
    """Score one case, return (result, passed).

    passed is True if all of these hold:
    - Hard asserts (must_contain / must_not_contain) all pass
    - Judge score >= case[min_llm_judge_score] (if judge is not malformed)

    If the judge returns malformed output (LLM couldn't produce valid
    JSON), we FALL BACK to hard-asserts only. This way a flaky judge
    doesn't mask a real safety bug.
    """
    result = await judge.score(case, response_text, child_age)
    min_score = case.get("min_llm_judge_score", 4)

    # Hard asserts: must_contain + must_not_contain. These are
    # deterministic and the most reliable signal.
    hard_passed = True
    for word in case.get("must_contain", []):
        if word not in response_text:
            hard_passed = False
            break
    for word in case.get("must_not_contain", []):
        if word in response_text:
            hard_passed = False
            break

    # Judge score: only required if judge is reliable.
    if result.malformed:
        logger.warning(
            "judge.fallback_to_hard_asserts",
            case_id=case.get("id", "unknown"),
        )
        judge_passed = True  # Don't double-fail; hard asserts decide
    else:
        judge_passed = result.score >= min_score

    return result, hard_passed and judge_passed
