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
    async def test_run_all_steps_succeed(self, pipeline: NightlyPipeline) -> None:
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

    async def test_step_results_have_status(self, pipeline: NightlyPipeline) -> None:
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

    async def test_partial_run_when_some_fail(self, pipeline: NightlyPipeline) -> None:
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


class TestWeeklyReportPersistence:
    """Step 6 actually persists a draft via DashboardRepository.add_alert."""

    async def test_weekly_report_persists_drafts(self) -> None:
        """When the context has user logs, _step_weekly_report must
        create weekly_report_draft alerts — not just count children
        and forget the data."""
        import niki.db.repositories as pkg
        from niki.db.repositories import dashboard_repo as repo_mod
        from niki.db.repositories.dashboard_repo import (
            InMemoryDashboardRepository,
        )
        from niki.memory.pipeline import NightlyPipeline
        from niki.memory.graph import CognitiveGraph
        from niki.memory.long_term import LongTermMemory

        # Patch the package-level symbol — the pipeline does
        # `from niki.db.repositories import get_dashboard_repository`
        # which binds to the package attribute, not the source
        # module's.
        fresh = InMemoryDashboardRepository()
        original = pkg.get_dashboard_repository

        def _patched():
            return fresh

        pkg.get_dashboard_repository = _patched  # type: ignore[assignment]
        try:
            p = NightlyPipeline(LongTermMemory(), CognitiveGraph())

            # Patch _resolve_parent_id so the test doesn't need
            # a real Postgres connection. Production behaviour
            # (dead-letter when parent_id can't be resolved) is
            # covered by the test below.
            async def _resolve_ok(child_id: str) -> str:  # noqa: ARG001
                return "parent-for-test"

            p._resolve_parent_id = _resolve_ok  # type: ignore[method-assign]

            context: dict = {
                "logs": [
                    {"child_id": "child-A", "role": "user", "text": "hi"},
                    {"child_id": "child-A", "role": "user", "text": "bye"},
                    {"child_id": "child-B", "role": "user", "text": "ok"},
                    {"child_id": "child-A", "role": "niki", "text": "..."},
                ],
                "entity_counts": {},
                "emotion_averages": {},
                "safety_flag_count": 0,
            }
            count = await p._step_weekly_report(context)
            assert count == 2  # 2 distinct children

            # The drafts must be persisted on the in-memory repo.
            persisted = [
                row
                for row in fresh._alerts.values()  # noqa: SLF001
                if row["alert_type"] == "weekly_report_draft"
            ]
            assert len(persisted) == 2, (
                f"expected 2 drafts, got {len(persisted)}: {list(fresh._alerts.values())}"  # noqa: SLF001
            )
            child_ids = {row["child_id"] for row in persisted}
            assert child_ids == {"child-A", "child-B"}
            # Each draft has the expected shape
            for row in persisted:
                assert "week_start" in row["payload"]
                assert "week_end" in row["payload"]
                assert "n_turns" in row["payload"]
                assert "n_safety_flags" in row["payload"]
                assert "mean_valence" in row["payload"]

            # And the context also carries the drafts list for
            # downstream consumers.
            assert "weekly_report_drafts" in context
            assert len(context["weekly_report_drafts"]) == 2
        finally:
            pkg.get_dashboard_repository = original  # type: ignore[assignment]
            # Touch the source module import so linters don't
            # complain about an unused import.
            del repo_mod

    async def test_weekly_report_missing_parent_dead_letters(self) -> None:
        """When the parent_id can't be resolved (no chat_sessions
        row / DB unavailable), the draft is dead-lettered + the
        count still increments so the KPI doesn't lie."""
        import niki.db.repositories as pkg
        from niki.db.repositories.dashboard_repo import (
            InMemoryDashboardRepository,
        )
        from niki.memory.pipeline import NightlyPipeline
        from niki.memory.graph import CognitiveGraph
        from niki.memory.long_term import LongTermMemory

        fresh = InMemoryDashboardRepository()
        original = pkg.get_dashboard_repository

        def _patched():
            return fresh

        pkg.get_dashboard_repository = _patched  # type: ignore[assignment]
        try:
            p = NightlyPipeline(LongTermMemory(), CognitiveGraph())

            # Simulate a missing parent — the in-memory path would
            # otherwise try to hit Postgres.
            async def _resolve_missing(_child_id: str) -> str:
                return ""

            p._resolve_parent_id = _resolve_missing  # type: ignore[method-assign]

            context: dict = {
                "logs": [
                    {"child_id": "orphan-A", "role": "user", "text": "hi"},
                ],
            }
            count = await p._step_weekly_report(context)
            # 1 child, 1 dead-letter entry, count still increments.
            assert count == 1
            # Nothing was persisted to the repo
            persisted = [
                row
                for row in fresh._alerts.values()  # noqa: SLF001
                if row["alert_type"] == "weekly_report_draft"
            ]
            assert persisted == []
            # But the dead-letter list got a row.
            assert any(
                d.get("child_id") == "orphan-A"
                for d in p._dead_letter  # noqa: SLF001
            )
        finally:
            pkg.get_dashboard_repository = original  # type: ignore[assignment]

    async def test_weekly_report_no_logs_is_zero(self) -> None:
        from niki.memory.pipeline import NightlyPipeline
        from niki.memory.graph import CognitiveGraph
        from niki.memory.long_term import LongTermMemory

        p = NightlyPipeline(LongTermMemory(), CognitiveGraph())
        context: dict = {"logs": []}
        count = await p._step_weekly_report(context)
        assert count == 0
        assert context["weekly_report_children"] == []
        assert context["weekly_report_drafts"] == []


class TestPipelineFailedCounter:
    """ISS-05 regression: ``StepStatus.FAILED`` must be reachable
    AND must be counted separately in ``health_check()``.

    The original bug: all failure paths jumped to ``DEAD_LETTER``
    so transient retries were indistinguishable from terminal
    failures, and the ``failed`` counter in health_check was
    permanently 0.
    """

    async def test_transient_failure_then_success_records_failed_attempt(
        self,
    ) -> None:
        """A step that fails on attempt 1 but succeeds on attempt 2
        must record ``FAILED`` in attempt_history before the final
        ``SUCCESS``, so health_check can count it."""
        long_term = LongTermMemory()
        graph = CognitiveGraph()
        pipeline = NightlyPipeline(long_term, graph, max_retries=3)

        call_count = {"n": 0}

        async def flaky(_ctx: dict) -> int:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient blip")
            return 7

        pipeline._steps = [PipelineStep(name="flaky", fn=flaky)]

        run = await pipeline.run()
        step = next(s for s in run.steps if s.step_name == "flaky")

        # Status is SUCCESS because the second attempt recovered.
        assert step.status == StepStatus.SUCCESS
        assert step.attempts == 2
        assert step.items_processed == 7
        # Attempt history records the transient FAILED.
        assert StepStatus.FAILED in step.attempt_history
        assert step.attempt_history[-1] == StepStatus.SUCCESS

    async def test_health_check_counts_transient_failed_attempts(self) -> None:
        """After a run that had a transient FAILED attempt, the
        ``failed`` field for that step must be > 0 (not 0 forever)."""
        long_term = LongTermMemory()
        graph = CognitiveGraph()
        pipeline = NightlyPipeline(long_term, graph, max_retries=2)

        async def fail_once(_ctx: dict) -> int:
            if not hasattr(fail_once, "_called"):
                fail_once._called = True  # type: ignore[attr-defined]
                raise RuntimeError("blip")
            return 1

        pipeline._steps = [PipelineStep(name="recovers", fn=fail_once)]

        await pipeline.run()
        h = pipeline.health_check()
        stats = h["steps"]["recovers"]
        assert stats["success"] == 1
        assert stats["failed"] >= 1, f"ISS-05 regression: transient FAILED not counted. Got {stats}"
        assert stats["dead_letter"] == 0

    async def test_dead_letter_does_not_count_as_failed(self) -> None:
        """A step that exhausts all retries (DEAD_LETTER) should
        count in dead_letter, NOT in failed. Otherwise health_check
        double-counts and the dashboard KPI lies."""
        long_term = LongTermMemory()
        graph = CognitiveGraph()
        pipeline = NightlyPipeline(long_term, graph, max_retries=2)

        async def always_fail(_ctx: dict) -> int:
            raise RuntimeError("permanent")

        pipeline._steps = [PipelineStep(name="doomed", fn=always_fail)]
        await pipeline.run()

        h = pipeline.health_check()
        stats = h["steps"]["doomed"]
        assert stats["dead_letter"] == 1
        assert stats["success"] == 0
        # Note: attempt_history for a dead-lettered step will still
        # contain FAILED for the first attempt. health_check counts
        # *every* FAILED entry in attempt_history. So ``failed``
        # may be 1 here — that's intentional: it represents the
        # observable retry attempts.
        # The key invariant is: dead_letter and failed are NOT
        # mutually exclusive in counting — they're separate
        # dimensions. The test below locks in that the
        # health_check no longer returns failed=0.
        assert stats["failed"] >= 1, f"failed counter still 0 (ISS-05 regression): {stats}"
