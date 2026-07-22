"""Multi-agent router — PRD routing flow.

Routes classified intents to the appropriate agent:
- Single intent → route to that agent directly
- Composite intent (multiple high-confidence categories)
  → invoke multiple agents in parallel → merge results

Design:
- Async
- Returns RoutingDecision with target agent name(s) + dispatch plan
- Does NOT call the agents itself (that happens elsewhere); it just
  decides which agents to call and in what order
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from niki.routing.classifier import (
    Intent,
    IntentCategory,
    IntentResult,
)

logger = structlog.get_logger(__name__)


class AgentName(StrEnum):
    """The available agents in the system."""

    ENCYCLOPEDIA = "encyclopedia_agent"
    TUTORING = "tutoring_agent"
    EMOTIONAL = "emotional_agent"
    COMPOSITE = "composite_merger"


@dataclass(frozen=True)
class RoutingDecision:
    """The routing outcome for a single child input."""

    primary_agent: AgentName
    parallel_agents: list[AgentName] = field(default_factory=list)
    intent_result: IntentResult | None = None
    rationale: str = ""

    @property
    def is_composite(self) -> bool:
        return bool(self.parallel_agents)

    @property
    def all_agents(self) -> list[AgentName]:
        """Return all agents that should be invoked, in order."""
        if not self.parallel_agents:
            return [self.primary_agent]
        return [self.primary_agent, *self.parallel_agents]


# Maps intent category → agent name
_INTENT_TO_AGENT: dict[IntentCategory, AgentName] = {
    IntentCategory.ENCYCLOPEDIA: AgentName.ENCYCLOPEDIA,
    IntentCategory.TUTORING: AgentName.TUTORING,
    IntentCategory.EMOTIONAL: AgentName.EMOTIONAL,
}


class AgentRouter:
    """Decides which agent(s) should handle a classified intent.

    Usage:
        router = AgentRouter()
        decision = router.route(intent_result)
        # dispatch to decision.all_agents ...
    """

    def __init__(
        self,
        enable_composite: bool = True,
        composite_threshold: float = 0.6,
    ) -> None:
        self._enable_composite = enable_composite
        self._composite_threshold = composite_threshold

    def route(self, intent_result: IntentResult) -> RoutingDecision:
        """Compute a RoutingDecision for the given IntentResult.

        Rules:
        - Empty intents → emotion_agent (catch-all, never leave the child hanging)
        - Single high-confidence intent → that agent only
        - Multiple high-confidence intents → primary agent + parallel agents
          in confidence-descending order. The first emotional intent always
          goes first (per PRD: 先情感共情→再切入解题).
        """
        if not intent_result.intents:
            logger.info("router.no_intents_fallback_to_emotion")
            return RoutingDecision(
                primary_agent=AgentName.EMOTIONAL,
                rationale="No intents detected — default to emotional support",
            )

        active = intent_result.above_threshold()
        if not active:
            active = [intent_result.primary] if intent_result.primary else []
            if not active:
                return RoutingDecision(
                    primary_agent=AgentName.EMOTIONAL,
                    rationale="All confidences below threshold — default to emotional",
                )

        # Sort: emotional first (empathy gate), then by confidence desc
        ordered = self._order_intents(active)

        primary_intent = ordered[0]
        parallel_intents = ordered[1:] if self._enable_composite and len(ordered) > 1 else []

        primary_agent = _INTENT_TO_AGENT[primary_intent.category]
        parallel_agents = [_INTENT_TO_AGENT[i.category] for i in parallel_intents]

        decision = RoutingDecision(
            primary_agent=primary_agent,
            parallel_agents=parallel_agents,
            intent_result=intent_result,
            rationale=self._build_rationale(primary_intent, parallel_intents),
        )

        logger.info(
            "router.decision",
            primary=primary_agent.value,
            parallel=[a.value for a in parallel_agents],
            is_composite=decision.is_composite,
        )

        return decision

    @staticmethod
    def _order_intents(active: list[Intent]) -> list[Intent]:
        """Order intents: emotional first, then by confidence desc."""
        emotional = [i for i in active if i.category == IntentCategory.EMOTIONAL]
        non_emotional = [i for i in active if i.category != IntentCategory.EMOTIONAL]
        emotional.sort(key=lambda i: -i.confidence)
        non_emotional.sort(key=lambda i: -i.confidence)
        return [*emotional, *non_emotional]

    @staticmethod
    def _build_rationale(primary: Intent, parallels: list[Intent]) -> str:
        parts = [f"Primary: {primary.category.value} ({primary.confidence:.2f})"]
        for p in parallels:
            parts.append(f"Parallel: {p.category.value} ({p.confidence:.2f})")
        return "; ".join(parts)
