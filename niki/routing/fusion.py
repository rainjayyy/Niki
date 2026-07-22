"""Fusion orchestrator — merges multi-agent outputs into a single child-facing reply.

PRD composite-intent flow:
    1. Input safety gateway (already in place — #1 slice)
    2. Intent classifier (top-level)
    3. Composite intent detected → invoke multiple agents in parallel
    4. Fusion orchestrator merges agent outputs

Order rule (PRD): "先情感共情→再切入解题"
When emotional + tutoring, the emotional response goes FIRST, then the tutoring
guidance is appended. For tutoring + encyclopedia, ordering is by confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog

from niki.routing.router import AgentName, RoutingDecision
from niki.schemas.common import AgentOutput

logger = structlog.get_logger(__name__)


class FusionStrategy(StrEnum):
    """How multiple agent outputs should be combined."""

    CONCATENATE = "concatenate"   # Sequential: A then B (empathy-first for emotional+tutoring)
    INTERLEAVE = "interleave"     # Alternate sentences (advanced; not used in #2 slice)
    PICK_PRIMARY = "pick_primary" # Use only the primary agent's output


@dataclass
class FusedOutput:
    """Result of merging multiple agent outputs."""

    text: str
    strategy: FusionStrategy
    contributing_agents: list[AgentName]
    primary_output: AgentOutput | None = None
    parallel_outputs: list[AgentOutput] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.parallel_outputs is None:
            self.parallel_outputs = []


class FusionOrchestrator:
    """Merges AgentOutputs from multiple agents into a single response.

    Strategy is selected automatically based on the RoutingDecision:
      - Single agent → PICK_PRIMARY (no actual merging)
      - Emotional + Tutoring → CONCATENATE with empathy-first ordering
      - Tutoring + Encyclopedia → CONCATENATE in confidence order
    """

    def fuse(
        self,
        decision: RoutingDecision,
        outputs: dict[AgentName, AgentOutput],
    ) -> FusedOutput:
        """Merge agent outputs.

        Args:
            decision: The routing decision that dispatched the agents.
            outputs:  Map from agent name to its output.

        Returns:
            A single FusedOutput ready for the output safety gateway.
        """
        if not decision.is_composite:
            # No merging needed
            primary_output = outputs.get(decision.primary_agent)
            text = primary_output.text if primary_output else ""
            return FusedOutput(
                text=text,
                strategy=FusionStrategy.PICK_PRIMARY,
                contributing_agents=[decision.primary_agent],
                primary_output=primary_output,
            )

        # Composite — concatenate in the order dictated by the router
        ordered_agents = decision.all_agents
        strategy = FusionStrategy.CONCATENATE
        parts: list[str] = []
        contributing: list[AgentName] = []
        primary_out: AgentOutput | None = None
        parallel_out: list[AgentOutput] = []

        for idx, agent in enumerate(ordered_agents):
            output = outputs.get(agent)
            if output is None or not output.text:
                continue
            if idx == 0:
                primary_out = output
            else:
                parallel_out.append(output)
            parts.append(output.text)
            contributing.append(agent)

        merged_text = " ".join(parts) if parts else ""

        logger.info(
            "fusion.complete",
            strategy=strategy.value,
            contributing=[a.value for a in contributing],
            text_length=len(merged_text),
        )

        return FusedOutput(
            text=merged_text,
            strategy=strategy,
            contributing_agents=contributing,
            primary_output=primary_out,
            parallel_outputs=parallel_out,
        )
