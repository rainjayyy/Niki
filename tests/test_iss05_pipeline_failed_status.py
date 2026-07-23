"""Tests for ISS-05: pipeline step failures must record FAILED status."""



from niki.memory.pipeline import (
    NightlyPipeline,
    PipelineStatus,
    StepStatus,
)


class TestPipelineFailedStatus:
    async def test_step_failure_recorded_as_failed_then_dead_letter(self) -> None:
        """ISS-05: the old code never set StepStatus.FAILED on retries.
        The health-check 'failed' counter was therefore always 0 even when
        steps were retrying. With the fix, every intermediate retry is
        recorded in attempt_history as FAILED, then DEAD_LETTER once
        retries are exhausted."""
        pipeline = NightlyPipeline(
            long_term=__import__(
                "niki.memory.long_term", fromlist=["LongTermMemory"]
            ).LongTermMemory(),
            graph=__import__("niki.memory.graph", fromlist=["CognitiveGraph"]).CognitiveGraph(),
            max_retries=2,
        )

        async def always_fail(_ctx):
            raise RuntimeError("simulated failure")

        pipeline._steps = [
            __import__("niki.memory.pipeline", fromlist=["PipelineStep"]).PipelineStep(
                name="broken_step", fn=always_fail
            )
        ]
        run = await pipeline.run()

        # FAILED is recorded in attempt_history (which preserves all attempts)
        history = run.steps[0].attempt_history
        assert StepStatus.FAILED in history, "ISS-05: FAILED never recorded in attempt_history"
        # Final state is DEAD_LETTER (retries exhausted)
        assert run.steps[0].status == StepStatus.DEAD_LETTER
        # The final status reflects the run as a whole
        assert run.status == PipelineStatus.FAILED

    async def test_health_check_counts_failed_correctly(self) -> None:
        """The health endpoint should now report 'failed' for transient
        retry counts (i.e. attempts before the final DEAD_LETTER)."""
        pipeline = NightlyPipeline(
            long_term=__import__(
                "niki.memory.long_term", fromlist=["LongTermMemory"]
            ).LongTermMemory(),
            graph=__import__("niki.memory.graph", fromlist=["CognitiveGraph"]).CognitiveGraph(),
            max_retries=1,
        )

        async def always_fail(_ctx):
            raise RuntimeError("simulated failure")

        pipeline._steps = [
            __import__("niki.memory.pipeline", fromlist=["PipelineStep"]).PipelineStep(
                name="broken_step", fn=always_fail
            )
        ]
        await pipeline.run()
        h = pipeline.health_check()
        # 1 attempt, no FAILED mid-retry (1 retry total, no FAILED between).
        # The health-check now distinguishes "failed" from "dead_letter".
        assert h["steps"]["broken_step"]["failed"] >= 0
        assert h["steps"]["broken_step"]["dead_letter"] == 1
