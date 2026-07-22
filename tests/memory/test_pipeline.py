"""Tests for the nightly compression pipeline."""

import pytest

from niki.memory.graph import CognitiveGraph
from niki.memory.long_term import LongTermMemory
from niki.memory.pipeline import (
    NightlyPipeline,
    PipelineStatus,
    PipelineStep,
    StepStatus,
)


@pytest.fixture
def pipeline() -> NightlyPipeline:
    long_term = LongTermMemory()
    graph = CognitiveGraph()
    return NightlyPipeline(long_term, graph)


class TestPipelineExecution:
    async def test_run_all_steps_succeed(
        self, pipeline: NightlyPipeline
    ) -> None:
        run = await pipeline.run()
        assert run.status == PipelineStatus.SUCCESS
        assert len(run.steps) >= 5

    async def test_all_steps_recorded(self, pipeline: NightlyPipeline) -> None:
        run = await pipeline.run()
        step_names = {s.step_name for s in run.steps}
        assert "read_conversation_log" in step_names
        assert "deep_extraction" in step_names
        assert "update_long_term" in step_names
        assert "update_cognitive_graph" in step_names
        assert "update_emotion_window" in step_names
        assert "calculate_baseline" in step_names

    async def test_step_results_have_status(
        self, pipeline: NightlyPipeline
    ) -> None:
        run = await pipeline.run()
        for step in run.steps:
            assert step.status == StepStatus.SUCCESS
            assert step.duration_ms >= 0
            assert step.attempts >= 1


class TestPipelineRetries:
    async def test_failing_step_retries(self) -> None:
        long_term = LongTermMemory()
        graph = CognitiveGraph()
        pipeline = NightlyPipeline(long_term, graph, max_retries=2)

        # Add a step that always fails
        async def always_fail(_ctx):
            raise RuntimeError("forced failure")

        pipeline._steps.insert(0, PipelineStep(name="broken_step", fn=always_fail))
        run = await pipeline.run()
        broken_result = next(s for s in run.steps if s.step_name == "broken_step")
        assert broken_result.status == StepStatus.DEAD_LETTER
        assert broken_result.attempts == 2
        assert "forced failure" in broken_result.error

    async def test_partial_run_when_some_fail(
        self, pipeline: NightlyPipeline
    ) -> None:
        # Add one failing step in the middle
        async def always_fail(_ctx):
            raise RuntimeError("oh no")

        pipeline._steps.insert(2, PipelineStep(name="doomed", fn=always_fail))
        run = await pipeline.run()
        assert run.status == PipelineStatus.PARTIAL


class TestDeadLetterQueue:
    async def test_dead_letter_appends(self) -> None:
        long_term = LongTermMemory()
        graph = CognitiveGraph()
        pipeline = NightlyPipeline(long_term, graph, max_retries=1)

        async def fail(_ctx):
            raise RuntimeError("x")

        pipeline._steps = [PipelineStep(name="f", fn=fail)]
        await pipeline.run()
        dl = pipeline.get_dead_letter()
        assert len(dl) == 1
        assert dl[0]["step_name"] == "f"


class TestPipelineHealth:
    async def test_health_check_no_runs(self, pipeline: NightlyPipeline) -> None:
        h = pipeline.health_check()
        assert h["total_runs"] == 0
        assert h["dead_letter_count"] == 0

    async def test_health_check_after_runs(self, pipeline: NightlyPipeline) -> None:
        await pipeline.run()
        await pipeline.run()
        h = pipeline.health_check()
        assert h["total_runs"] == 2
        # Each step should have 2 successes
        for stats in h["steps"].values():
            assert stats["success"] == 2

    async def test_health_last_run_returned(self, pipeline: NightlyPipeline) -> None:
        await pipeline.run()
        h = pipeline.health_check()
        assert h["last_run"] is not None
        assert h["last_run"]["status"] == "success"

    def test_disabled_step_skipped(self) -> None:
        async def noop(_ctx):
            return 0

        p = NightlyPipeline(LongTermMemory(), CognitiveGraph())
        p._steps = [PipelineStep(name="disabled", fn=noop, enabled=False)]
        # Run via event loop in a test
        import asyncio
        run = asyncio.run(p.run())
        assert all(s.step_name != "disabled" for s in run.steps)
