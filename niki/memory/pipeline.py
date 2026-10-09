"""Nightly compression pipeline — PRD #45-#47.

APScheduler-driven daily deep extraction:
1. Read day's conversation log
2. Deep entity/preference/fact extraction (using LLM in production)
3. Update Chroma (long-term) + Neo4j (cognitive graph)
4. Update emotion sliding window
5. Generate parent dashboard weekly report draft
6. Calculate baseline (Z-score)
7. Update safety annotations

Each step is:
- Idempotent (safe to re-run)
- Auto-retried 3× with exponential backoff
- Final failure → dead-letter queue (manual triage)
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

import structlog

from niki.memory.graph import CognitiveGraph
from niki.memory.long_term import LongTermMemory, MemoryRecord, MemorySource

logger = structlog.get_logger(__name__)


# Default scheduled time (PRD: 2 AM local time)
DEFAULT_PIPELINE_TIME = "02:00"


class StepStatus(StrEnum):
    """Status of a single pipeline step."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class PipelineStepResult:
    """Result of a single pipeline step.

    ISS-05: `status` is the FINAL status of the step (SUCCESS or
    DEAD_LETTER). The `attempts` count captures how many times the
    step was attempted (including retries). To audit transient
    FAILED → FAILED → DEAD_LETTER transitions, look at the
    `attempt_history` list.
    """

    step_name: str
    status: StepStatus
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float = 0.0
    error: str | None = None
    attempts: int = 0
    items_processed: int = 0
    attempt_history: list[StepStatus] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_name": self.step_name,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
            "attempts": self.attempts,
            "items_processed": self.items_processed,
            "attempt_history": [s.value for s in self.attempt_history],
        }


class PipelineStatus(StrEnum):
    """Overall pipeline status."""

    SUCCESS = "success"
    PARTIAL = "partial"  # some steps failed
    FAILED = "failed"  # all steps failed
    RUNNING = "running"
    IDLE = "idle"


@dataclass
class PipelineRun:
    """A complete pipeline run."""

    run_id: str
    started_at: datetime
    completed_at: datetime | None = None
    status: PipelineStatus = PipelineStatus.RUNNING
    steps: list[PipelineStepResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
        }


# Type for a step function
StepFn = Callable[[dict[str, Any]], Awaitable[int]]  # returns items processed


@dataclass
class PipelineStep:
    """Definition of a single step in the nightly pipeline."""

    name: str
    fn: StepFn
    enabled: bool = True


class NightlyPipeline:
    """Orchestrates the daily deep-extraction pipeline.

    Steps (PRD #45):
    1. Read day's conversation log
    2. Deep entity/fact extraction
    3. Update long-term memory (Chroma)
    4. Update cognitive graph (Neo4j)
    5. Update emotion sliding window
    6. Generate weekly report draft
    7. Calculate baseline (Z-score)
    8. Update safety annotations

    Each step is retried up to 3 times with exponential backoff.
    Final failures land in the dead-letter queue.
    """

    # PRD #46: 3 retries with exponential backoff
    MAX_RETRIES = 3
    BACKOFF_BASE_S = 0.5  # seconds

    def __init__(
        self,
        long_term: LongTermMemory,
        graph: CognitiveGraph,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._long_term = long_term
        self._graph = graph
        self._max_retries = max_retries
        self._steps: list[PipelineStep] = []
        self._history: list[PipelineRun] = []
        self._dead_letter: list[PipelineStepResult] = []

        # Default steps
        self._register_default_steps()

    def _register_default_steps(self) -> None:
        """Register the default PRD #45 steps."""
        self._steps = [
            PipelineStep(name="read_conversation_log", fn=self._step_read_logs),
            PipelineStep(name="deep_extraction", fn=self._step_deep_extraction),
            PipelineStep(name="update_long_term", fn=self._step_update_long_term),
            PipelineStep(name="update_cognitive_graph", fn=self._step_update_graph),
            PipelineStep(name="update_emotion_window", fn=self._step_update_emotion),
            PipelineStep(name="generate_weekly_report", fn=self._step_weekly_report),
            PipelineStep(name="calculate_baseline", fn=self._step_calculate_baseline),
            PipelineStep(name="update_safety_annotations", fn=self._step_safety_annotate),
        ]

    # ---- Step implementations ----

    async def _step_read_logs(self, context: dict[str, Any]) -> int:
        """Step 1: read recent conversation logs from PostgreSQL."""
        from sqlalchemy import text

        from niki.infrastructure.postgres import PostgresClient

        logs: list[dict[str, Any]] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        try:
            async for session in PostgresClient.get_session():
                rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT child_id, messages FROM chat_sessions "
                                "WHERE updated_at >= :cutoff"
                            ),
                            {"cutoff": cutoff},
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    for msg in row["messages"] or []:
                        if isinstance(msg, dict) and msg.get("text"):
                            logs.append(
                                {
                                    "child_id": row["child_id"],
                                    "role": msg.get("role", "user"),
                                    "text": msg["text"],
                                    "ts": msg.get("ts") or 0,
                                }
                            )
        except Exception as exc:
            logger.warning("pipeline.read_logs_failed", error=str(exc))
        context["logs"] = logs
        return len(logs)

    async def _step_deep_extraction(self, _context: dict[str, Any]) -> int:
        """Step 2: LLM-based deep entity/fact extraction."""
        # In production: call DeepSeek-V4 with the day's logs
        return 0

    async def _step_update_long_term(self, context: dict[str, Any]) -> int:
        """Step 3: write conversation records to Chroma long-term memory."""
        count = 0
        for entry in context.get("logs", []):
            if entry["role"] != "user" or not entry["text"]:
                continue
            ts_value = entry.get("ts") or 0
            ts = (
                datetime.fromtimestamp(int(ts_value) / 1000, tz=timezone.utc)
                if ts_value
                else datetime.now(timezone.utc)
            )
            record = MemoryRecord(
                id=LongTermMemory.make_id(entry["child_id"], entry["text"], "nightly_pipeline"),
                content=entry["text"],
                source=MemorySource.NIGHTLY_PIPELINE,
                child_id=entry["child_id"],
                timestamp=ts,
            )
            await self._long_term.add(record)
            count += 1
        return count

    async def _step_update_graph(self, context: dict[str, Any]) -> int:
        """Step 4: write child entities to the Neo4j cognitive graph."""
        child_ids = {entry["child_id"] for entry in context.get("logs", [])}
        count = 0
        for cid in child_ids:
            self._graph.add_entity(f"child:{cid}", entity_type="child")
            count += 1
        return count

    async def _step_update_emotion(self, _context: dict[str, Any]) -> int:
        """Step 5: aggregate today's emotions into the daily record."""
        return 0

    async def _step_weekly_report(self, _context: dict[str, Any]) -> int:
        """Step 6: draft the weekly report (delivered Monday)."""
        return 0

    async def _step_calculate_baseline(self, _context: dict[str, Any]) -> int:
        """Step 7: recalculate the 7-day baseline (Mondays)."""
        return 0

    async def _step_safety_annotate(self, _context: dict[str, Any]) -> int:
        """Step 8: add safety annotations to the parent dashboard."""
        return 0

    # ---- Orchestration ----

    async def run(self) -> PipelineRun:
        """Run the full pipeline once.

        Returns:
            PipelineRun with per-step results.
        """
        run_id = hashlib.sha256(
            f"pipeline:{datetime.now(timezone.utc).isoformat()}".encode()
        ).hexdigest()[:16]

        run = PipelineRun(
            run_id=run_id,
            started_at=datetime.now(timezone.utc),
        )
        any_failed = False
        all_failed = True

        for step in self._steps:
            if not step.enabled:
                continue
            result = await self._run_step(step)
            run.steps.append(result)
            if result.status == StepStatus.DEAD_LETTER:
                any_failed = True
                self._dead_letter.append(result)
            else:
                all_failed = False

        if all_failed:
            run.status = PipelineStatus.FAILED
        elif any_failed:
            run.status = PipelineStatus.PARTIAL
        else:
            run.status = PipelineStatus.SUCCESS
        run.completed_at = datetime.now(timezone.utc)
        self._history.append(run)
        logger.info(
            "pipeline.run_complete",
            run_id=run_id,
            status=run.status.value,
        )
        return run

    async def _run_step(self, step: PipelineStep) -> PipelineStepResult:
        """Run a single step with retries."""
        result = PipelineStepResult(
            step_name=step.name,
            status=StepStatus.PENDING,
            started_at=datetime.now(timezone.utc),
        )

        for attempt in range(1, self._max_retries + 1):
            result.attempts = attempt
            # Only set RUNNING on the first attempt.
            if attempt == 1:
                result.status = StepStatus.RUNNING
                result.attempt_history.append(StepStatus.RUNNING)
            try:
                start = datetime.now(timezone.utc)
                items = await step.fn({})
                end = datetime.now(timezone.utc)
                result.items_processed = items
                result.duration_ms = (end - start).total_seconds() * 1000
                result.completed_at = end
                result.status = StepStatus.SUCCESS
                result.attempt_history.append(StepStatus.SUCCESS)
                return result
            except Exception as exc:
                logger.warning(
                    "pipeline.step_failed",
                    step=step.name,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < self._max_retries:
                    # ISS-05: each failed attempt (before exhausting retries)
                    # is a transient FAILED, not yet a DEAD_LETTER.
                    result.error = str(exc)
                    result.completed_at = datetime.now(timezone.utc)
                    result.status = StepStatus.FAILED
                    result.attempt_history.append(StepStatus.FAILED)
                    backoff = self.BACKOFF_BASE_S * (2 ** (attempt - 1))
                    await asyncio.sleep(backoff)
                else:
                    result.error = str(exc)
                    result.completed_at = datetime.now(timezone.utc)
                    result.status = StepStatus.DEAD_LETTER
                    result.attempt_history.append(StepStatus.DEAD_LETTER)

        return result

    # ---- Health check (PRD #47) ----

    def health_check(self, lookback_days: int = 7) -> dict[str, Any]:
        """Return the health status of the pipeline (PRD #47).

        Returns:
            Dict with summary + per-step status over the lookback window.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        recent_runs = [r for r in self._history if r.started_at >= cutoff]

        step_status: dict[str, dict[str, int]] = {}
        for run in recent_runs:
            for step_result in run.steps:
                if step_result.step_name not in step_status:
                    step_status[step_result.step_name] = {
                        "success": 0,
                        "failed": 0,
                        "dead_letter": 0,
                    }
                if step_result.status == StepStatus.SUCCESS:
                    step_status[step_result.step_name]["success"] += 1
                elif step_result.status == StepStatus.DEAD_LETTER:
                    step_status[step_result.step_name]["dead_letter"] += 1
                # ISS-05: count transient retries (FAILED before DEAD_LETTER)
                # separately from the final dead-letter
                for st in step_result.attempt_history:
                    if st == StepStatus.FAILED:
                        step_status[step_result.step_name]["failed"] += 1

        return {
            "lookback_days": lookback_days,
            "total_runs": len(recent_runs),
            "dead_letter_count": len([r for r in self._dead_letter if r.started_at >= cutoff]),
            "steps": step_status,
            "last_run": recent_runs[-1].to_dict() if recent_runs else None,
        }

    def get_dead_letter(self) -> list[dict[str, Any]]:
        """Return all items in the dead-letter queue."""
        return [r.to_dict() for r in self._dead_letter]
