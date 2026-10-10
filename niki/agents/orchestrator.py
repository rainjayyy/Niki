"""Chat orchestrator — the end-to-end pipeline.

Pipeline (PRD composite-intent flow):
  1. Input safety gateway — sensitive-word filter, topic-blacklist
     downgrade, self-harm escalation.
  2. Privacy tier classifier — DAILY / MILD_NEGATIVE / HIGH_RISK /
     TREEHOLE_PRIVATE. HIGH_RISK fans out to parent alerts;
     TREEHOLE_PRIVATE engages the AES-256-GCM storage path.
  3. Intent classifier — top-level intent + tutoring subtype.
  4. Router — primary + parallel agents (empathy-first ordering).
  5. Persona injector — fills {persona} in each agent's prompt.
  6. Agent dispatch: Encyclopedia / Tutoring / Emotional.
  7. Fusion orchestrator — merge multi-agent outputs.
  8. Output safety gateway — final fear-word rewrite, age check.
  9. Return final text + diagnostics (including privacy tier).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

import structlog

from niki.agents.emotion import EmotionAgent, EmotionBaseline
from niki.agents.encyclopedia import EncyclopediaAgent
from niki.agents.tutoring import TutoringAgent
from niki.knowledge.fact_checker import FactChecker
from niki.knowledge.knowledge_base import KnowledgeBase
from niki.memory.short_term import (
    ConversationTurn,
    get_short_term_memory,
    turns_to_history,
)
from niki.routing import (
    AgentName,
    AgentRouter,
    FusionOrchestrator,
    IntentClassifier,
    Persona,
    PersonaInjector,
)
from niki.safety.input_gateway import InputSafetyGateway
from niki.safety.output_gateway import (
    OutputAction,
    OutputSafetyGateway,
    strip_think_blocks,
)
from niki.safety.privacy_tier import (
    PrivacyTier,
    PrivacyTierClassifier,
    PrivacyTierVerdict,
)
from niki.schemas.common import AgentOutput, ChildInput
from niki.tools import ToolAccessChecker, ToolMatrix

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class ChatStage(StrEnum):
    """Stages of the chat pipeline (for diagnostics + observability)."""

    SAFETY_INPUT = "safety_input"
    TIER_CLASSIFY = "tier_classify"
    CLASSIFY = "classify"
    ROUTE = "route"
    DISPATCH = "dispatch"
    FUSE = "fuse"
    SAFETY_OUTPUT = "safety_output"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ChatResult:
    """Final result of the chat pipeline."""

    text: str
    final_text: str  # After output safety gateway
    intent: str | None = None
    primary_agent: str | None = None
    parallel_agents: list[str] = field(default_factory=list)
    is_composite: bool = False
    safety_alert: bool = False
    parent_alert_required: bool = False
    # Privacy tier (PRD #5). ``None`` means tier classification was
    # skipped (e.g. blocked before the stage ran).
    privacy_tier: PrivacyTier | None = None
    privacy_tier_reason: str = ""
    treehole_engaged: bool = False
    stages_completed: list[ChatStage] = field(default_factory=list)
    safety_rewrites: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ChatOrchestrator:
    """End-to-end chat pipeline orchestrator.

    Composes:
    - InputSafetyGateway (#1)
    - IntentClassifier + AgentRouter + FusionOrchestrator (#2)
    - PersonaInjector (#2)
    - EncyclopediaAgent (#3) + TutoringAgent (#4) + EmotionAgent (#5)
    - OutputSafetyGateway (#1)
    """

    def __init__(
        self,
        child_age: int = 7,
        persona: str | None = "gentle",
        llm: object = None,
        *,
        dashboard_service: object = None,
    ) -> None:
        self.child_age = child_age
        self.persona = persona
        # BE-ISS-AUTOSAFE-VALVE: optional pre-built dashboard
        # service. When provided, round-4 safety-valve events are
        # auto-persisted (PRD #27) without the frontend needing
        # to call ``/dashboard/alerts/record/safety-valve``
        # separately. Production default: lazy-create one.
        self._dashboard_service = dashboard_service

        # Safety (#1)
        self._input_gateway = InputSafetyGateway()
        self._output_gateway = OutputSafetyGateway(child_age=child_age)

        # Privacy tier (#5) — classifies the message into one of
        # DAILY / MILD_NEGATIVE / HIGH_RISK / TREEHOLE_PRIVATE. The
        # tier drives parent-alert fan-out and treehole AES-256-GCM
        # engagement downstream.
        self._tier_classifier = PrivacyTierClassifier()

        # Routing (#2) — use rule-based classifier (LLM stub returns empty)
        self._classifier = IntentClassifier(llm=None)
        self._router = AgentRouter()
        self._fusion = FusionOrchestrator()
        self._persona_injector = PersonaInjector(
            default=Persona(persona) if persona else Persona.GENTLE
        )

        # Knowledge (#3) + Agents (#3, #4, #5)
        self._kb = KnowledgeBase()
        # BE-FACT-LLM-JUDGE: wire the configured LLM as the
        # fact-checker judge so production uses the LLM judge
        # path. We only do this when the injected LLM is a real
        # ``BaseLLMClient`` (production) — when tests inject a
        # recording stub that doesn't speak the JSON judge
        # protocol, we leave ``llm_judge=None`` so the
        # FactChecker falls back to the heuristic overlap path.
        # The heuristic path was the previous production
        # behaviour, so this is a no-op for tests that asserted
        # it directly.
        from niki.infrastructure.llm.base import BaseLLMClient

        is_real_llm = isinstance(llm, BaseLLMClient)
        self._fact_checker = FactChecker(
            self._kb,
            llm_judge=llm if is_real_llm else None,
        )
        self._emotion_baseline = EmotionBaseline()
        # BE-AGENT-001: shared tool-permission checker for the
        # LLM-driven tool-use loop. Constructed per-orchestrator
        # instance so the audit log is scoped to a single client
        # lifecycle (matrix itself is read-only).
        self._tool_checker = ToolAccessChecker(ToolMatrix())
        # ISS-12: lock for thread-safe _update_age/_update_persona
        self._lock = asyncio.Lock()

        # BE-ISS-AUTOSAFE-VALVE: dashboard service for auto-recording
        # tutoring safety-valve triggers. Lazy-imported to avoid a
        # hard import-time dependency on the dashboard layer. Tests
        # can inject a stub via the ``dashboard_service`` kwarg.
        self._dashboard_service = None
        # BE-ISS-MEMORY-HOOK: daytime memory hook for extracting
        # entities / emotions from each non-blocked message. Skipped
        # for TREEHOLE_PRIVATE tier (the original text must never
        # cross the treehole boundary — not even into a hook's
        # in-memory candidate list). Lazy-imported to keep the
        # orchestrator's import graph lean.
        self._memory_hook = None

        # Resolve the production LLM via the factory. Callers may inject
        # one (`llm=`), otherwise we ask the factory — which returns a
        # real MinimaxClient when MINIMAX_API_KEY is set, else the stub.
        from niki.infrastructure.llm.factory import get_or_create_llm

        self._llm = llm if llm is not None else get_or_create_llm()
        # The per-agent sub-LMs must expose `.chat(messages, ...)` — the
        # factory returns a BaseLLMClient which satisfies that. When the
        # factory is in stub mode we still pass it through (it echoes a
        # safe phrase), so the full safety+classify+dispatch pipeline is
        # exercisable without network.
        self._encyclopedia_agent = EncyclopediaAgent(
            llm=self._llm,
            knowledge_base=self._kb,
            fact_checker=self._fact_checker,
            persona=persona or "gentle",
            child_age=child_age,
            tool_checker=self._tool_checker,
        )
        self._tutoring_agent = TutoringAgent(
            llm=self._llm,
            persona=persona or "gentle",
            child_age=child_age,
        )
        self._emotion_agent = EmotionAgent(
            llm=self._llm,
            baseline=self._emotion_baseline,
            persona=persona or "gentle",
            child_age=child_age,
        )

    # ---- Main entry point ----

    async def chat(
        self,
        text: str,
        session_id: str = "default",
        child_age: int | None = None,
        persona: str | None = None,
        *,
        child_id: str | None = None,
        parent_id: str | None = None,
    ) -> ChatResult:
        """Run the full chat pipeline.

        Args:
            text: The child's input text.
            session_id: Session identifier (for state continuity).
            child_age: Override the default child age.
            persona: Override the default persona.
            child_id: Required for HIGH_RISK parent alerts and
                TREEHOLE_PRIVATE engagement. When ``None`` the
                tier is still classified but downstream side-effects
                (alert / treehole.engage) are skipped.
            parent_id: Required alongside ``child_id`` for the same
                side-effects.

        Returns:
            A ChatResult with final text + diagnostics.
        """
        if child_age is not None and child_age != self.child_age:
            await self._update_age(child_age)
        if persona is not None and persona != self.persona:
            await self._update_persona(persona)

        stages: list[ChatStage] = []

        # 1. Input safety (#1)
        input_verdict = await self._input_gateway.check(text)
        stages.append(ChatStage.SAFETY_INPUT)

        if input_verdict.is_blocked:
            logger.info(
                "chat.input_blocked",
                category=input_verdict.matched_category,
            )
            return ChatResult(
                text=text,
                final_text="嗯…这个问题Niki还没想好怎么回答呢~",
                intent=None,
                safety_alert=True,
                parent_alert_required=False,
                stages_completed=stages,
                metadata={
                    "input_action": input_verdict.action.value,
                    "input_category": input_verdict.matched_category,
                    "child_age": self.child_age,
                    "persona": self.persona,
                },
            )

        if input_verdict.is_escalation:
            # Self-harm — produce a caring response and flag parent alert
            caring_text = (
                "宝贝，Niki听到你说的话了。"
                "不管发生什么，Niki都在这里陪着你。"
                "我们一起想想办法，好不好？"
            )
            verdict = self._output_gateway.check(caring_text, is_tutoring_response=False)
            # PRD #5: tier still classified for downstream
            # dashboards, and HIGH_RISK side-effects are wired here
            # too so we don't double-classify.
            tier_verdict = self._tier_classifier.classify(text)
            stages.append(ChatStage.TIER_CLASSIFY)
            treehole_engaged = await self._dispatch_tier_side_effects(
                tier_verdict=tier_verdict,
                original_text=text,
                child_id=child_id,
                parent_id=parent_id,
            )
            return ChatResult(
                text=text,
                final_text=verdict.sanitized_text or caring_text,
                intent="emotional",
                primary_agent=AgentName.EMOTIONAL.value,
                safety_alert=True,
                parent_alert_required=True,
                privacy_tier=tier_verdict.tier,
                privacy_tier_reason=tier_verdict.reason,
                treehole_engaged=treehole_engaged,
                stages_completed=stages + [ChatStage.SAFETY_OUTPUT, ChatStage.COMPLETE],
                metadata={
                    "input_action": input_verdict.action.value,
                    "input_category": input_verdict.matched_category,
                    "privacy_tier": tier_verdict.tier.value,
                    "privacy_tier_reason": tier_verdict.reason,
                    "privacy_tier_category": tier_verdict.matched_category,
                    "child_age": self.child_age,
                    "persona": self.persona,
                },
            )

        # 2. Privacy tier classification (#5) — runs BEFORE intent
        # classification so HIGH_RISK can fan-out a parent alert
        # and TREEHOLE_PRIVATE can engage the treehole AES-256-GCM
        # storage path. Tier verdict is *orthogonal* to the
        # InputAction flow (which already handled block/escalate).
        tier_verdict = self._tier_classifier.classify(text)
        stages.append(ChatStage.TIER_CLASSIFY)
        treehole_engaged = await self._dispatch_tier_side_effects(
            tier_verdict=tier_verdict,
            original_text=text,
            child_id=child_id,
            parent_id=parent_id,
        )

        # 3. Intent classification (#2)
        intent_result = await self._classifier.classify(text)
        stages.append(ChatStage.CLASSIFY)

        # 3. Routing (#2)
        routing_decision = self._router.route(intent_result)
        stages.append(ChatStage.ROUTE)

        # 4. Agent dispatch (with the session's recent dialogue from
        # the Redis-backed ShortTermMemory sliding window)
        history = turns_to_history(await get_short_term_memory().get_recent(session_id))
        outputs = await self._dispatch(routing_decision, text, session_id, history)
        stages.append(ChatStage.DISPATCH)

        # 4b. BE-ISS-AUTOSAFE-VALVE: if the tutoring agent
        # triggered its safety valve (round 4), auto-persist the
        # alert so the parent weekly report picks it up
        # (PRD #27). The frontend previously had to make a
        # separate POST to /dashboard/alerts/record/safety-valve;
        # the orchestrator now does it as a side-effect of the
        # chat call, so the dashboard KPI is correct without
        # client cooperation. Failures are caught + logged so a
        # dashboard outage never blocks the chat reply.
        await self._maybe_record_safety_valve(
            outputs=outputs,
            child_id=child_id,
            parent_id=parent_id,
            problem_text=text,
        )

        # 4c. BE-ISS-MEMORY-HOOK: extract entities / emotions
        # from the message into the cognitive graph + long-term
        # memory. PRD #48 says < 500ms — the regex hook is
        # sub-ms. Skipped for TREEHOLE_PRIVATE so the raw text
        # never enters the graph or vector store. Errors are
        # logged, not raised.
        if tier_verdict.tier != PrivacyTier.TREEHOLE_PRIVATE:
            await self._maybe_run_memory_hook(
                child_id=child_id,
                text=text,
                context={
                    "intent": intent_result.primary.category.value
                    if intent_result.primary
                    else None
                },
            )

        # 4d. BE-ISS-NORTHSTAR: feed the EmotionAgent's anomaly
        # verdict into the AnomalyTracker. The tracker opens an
        # anomaly on |z|>1.5 and closes it on the next
        # in-baseline record — the elapsed time is the North
        # Star metric ("主动关怀后情绪恢复至基线时间"). Skipped
        # for TREEHOLE_PRIVATE because the baseline doesn't see
        # treehole text and the Z-score would be meaningless.
        if tier_verdict.tier != PrivacyTier.TREEHOLE_PRIVATE and child_id is not None:
            from datetime import datetime, timezone as _tz

            self._track_emotion_anomaly(
                outputs=outputs,
                child_id=child_id,
                today=datetime.now(_tz.utc),
            )

        # 5. Fusion
        fused = self._fusion.fuse(routing_decision, outputs)
        stages.append(ChatStage.FUSE)

        # 6. Output safety (#1)
        is_tutoring = routing_decision.primary_agent == AgentName.TUTORING
        output_verdict = self._output_gateway.check(fused.text, is_tutoring_response=is_tutoring)
        stages.append(ChatStage.SAFETY_OUTPUT)

        final_text = output_verdict.sanitized_text or fused.text
        if output_verdict.action == OutputAction.BLOCK:
            # Privacy reverse-question blocked
            final_text = "Niki觉得这个问题我们可以聊点别的呀~"

        # Remember this turn so the next message in the same session
        # can see the earlier conversation.
        memory = get_short_term_memory()
        await memory.append(
            session_id,
            ConversationTurn(role="child", text=text),
        )
        await memory.append(
            session_id,
            ConversationTurn(role="niki", text=final_text),
        )

        stages.append(ChatStage.COMPLETE)

        # Build diagnostic metadata
        metadata = {
            "input_action": input_verdict.action.value,
            "input_category": input_verdict.matched_category,
            "intent": intent_result.primary.category.value if intent_result.primary else None,
            "is_composite": routing_decision.is_composite,
            "fusion_strategy": fused.strategy.value,
            "contributing_agents": [a.value for a in fused.contributing_agents],
            "output_action": output_verdict.action.value,
            "output_reason": output_verdict.reason,
            "child_age": self.child_age,
            "persona": self.persona,
            "privacy_tier": tier_verdict.tier.value,
            "privacy_tier_reason": tier_verdict.reason,
            "privacy_tier_category": tier_verdict.matched_category,
        }

        return ChatResult(
            text=text,
            final_text=final_text,
            intent=cast("str | None", metadata["intent"]),
            primary_agent=routing_decision.primary_agent.value,
            parallel_agents=[a.value for a in routing_decision.parallel_agents],
            is_composite=routing_decision.is_composite,
            stages_completed=stages,
            safety_rewrites=[{"from": o, "to": r} for o, r in output_verdict.rewrites_applied],
            parent_alert_required=tier_verdict.requires_parent_alert,
            privacy_tier=tier_verdict.tier,
            privacy_tier_reason=tier_verdict.reason,
            treehole_engaged=treehole_engaged,
            metadata=metadata,
        )

    # ---- Internals ----

    async def _dispatch(
        self,
        decision: object,
        text: str,
        session_id: str,
        history: list[dict[str, str]],
    ) -> dict[AgentName, AgentOutput]:
        """Dispatch to each agent in the routing decision."""
        outputs: dict[AgentName, AgentOutput] = {}
        all_agents = decision.all_agents  # type: ignore[attr-defined]
        tasks = [self._invoke_agent(agent, text, session_id, history) for agent in all_agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for agent, result in zip(all_agents, results, strict=False):
            if isinstance(result, BaseException):
                # Catch BaseException (parent of Exception) for the
                # isinstance narrowing — otherwise mypy can't prove
                # result is AgentOutput when we access result.text.
                logger.error(
                    "chat.agent_failed",
                    agent=agent.value,
                    error=str(result),
                )
                continue
            if result is not None:
                # Defense in depth: strip <think>...</think> blocks
                # that reasoning models (Minimax-M3) emit before the
                # final answer. These must never reach the child.
                # BE-ISS-17: replace, do not mutate — agents that
                # cache and return the same AgentOutput for repeated
                # inputs would otherwise leak the cleaned text into
                # the next call.
                if result.text:
                    cleaned = strip_think_blocks(result.text)
                    if cleaned != result.text:
                        result = result.model_copy(update={"text": cleaned})
                outputs[agent] = result
        return outputs

    async def _invoke_agent(
        self,
        agent: AgentName,
        text: str,
        session_id: str,
        history: list[dict[str, str]],
    ) -> AgentOutput | None:
        """Invoke a single agent."""
        child_input = ChildInput(
            text=text,
            session_id=session_id,
            history=history,
        )  # type: ignore[call-arg]
        try:
            if agent == AgentName.ENCYCLOPEDIA:
                return await self._encyclopedia_agent.invoke(child_input)
            elif agent == AgentName.TUTORING:
                return await self._tutoring_agent.invoke(child_input)
            elif agent == AgentName.EMOTIONAL:
                return await self._emotion_agent.invoke(child_input)
            else:
                logger.warning("chat.unknown_agent", agent=agent.value)
                return None
        except Exception as exc:
            logger.error("chat.invoke_failed", agent=agent.value, error=str(exc))
            return None

    # ---- Post-dispatch side-effects (BE-ISS-AUTOSAFE-VALVE / BE-ISS-MEMORY-HOOK) ----

    def _get_dashboard_service(self) -> object:
        """Return the dashboard service, lazily creating a default one.

        The lazy pattern keeps ``niki.dashboard.service`` out of the
        orchestrator's import graph at module-load time (the
        dashboard layer imports ``niki.agents.emotion`` and we don't
        want to add another edge to the import DAG from a hot
        path).
        """
        if self._dashboard_service is None:
            from niki.dashboard.service import DashboardService

            self._dashboard_service = DashboardService()
        return self._dashboard_service

    def _get_memory_hook(self) -> object:
        """Return the daytime memory hook, lazily creating a default one.

        The hook writes to ``LongTermMemory`` (Chroma) and
        ``CognitiveGraph`` (Neo4j). Both fall back to in-memory
        storage when their respective services are unreachable,
        so the hook is safe to instantiate in dev / test.
        """
        if self._memory_hook is None:
            from niki.memory.hook import DaytimeMemoryHook
            from niki.memory.graph import CognitiveGraph
            from niki.memory.long_term import LongTermMemory

            self._memory_hook = DaytimeMemoryHook(
                long_term=LongTermMemory(),
                graph=CognitiveGraph(),
            )
        return self._memory_hook

    async def _maybe_record_safety_valve(
        self,
        *,
        outputs: dict[AgentName, AgentOutput],
        child_id: str | None,
        parent_id: str | None,
        problem_text: str,
    ) -> None:
        """Persist a tutoring safety-valve alert if the agent fired one.

        The Tutoring agent returns ``metadata["safety_valve_triggered"]``
        on round 4 (PRD #7). The orchestrator persists the event as
        a side-effect of the chat call so the parent weekly report
        stays correct without the frontend having to call the
        dashboard endpoint separately.

        Failures are caught + logged so a dashboard outage never
        blocks the chat reply.
        """
        if child_id is None or parent_id is None:
            return
        tutoring_output = outputs.get(AgentName.TUTORING)
        if tutoring_output is None:
            return
        if not (tutoring_output.metadata or {}).get("safety_valve_triggered"):
            return
        try:
            service = self._get_dashboard_service()
            # Truncate the problem text so the alert payload
            # doesn't carry a full child transcript. PRD privacy
            # rule: alert payloads store a *summary*, not raw text.
            summary = problem_text[:80].replace("\n", " ")
            await service.record_safety_valve(
                child_id=child_id,
                parent_id=parent_id,
                problem_summary=summary,
                round_reached=int((tutoring_output.metadata or {}).get("round", 4)),
            )
        except Exception:
            logger.exception("chat.safety_valve_persist_failed")

    async def _maybe_run_memory_hook(
        self,
        *,
        child_id: str | None,
        text: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Run the daytime memory hook for non-treehole messages.

        Skipped when ``child_id`` is missing (the hook is
        per-child) and silently swallows errors so a hook
        failure can never abort the chat pipeline.
        """
        if child_id is None:
            return
        try:
            hook = self._get_memory_hook()
            await hook.process(child_id=child_id, text=text, context=context)
        except Exception:
            logger.exception("chat.memory_hook_failed")

    def _track_emotion_anomaly(
        self,
        *,
        outputs: dict[AgentName, AgentOutput],
        child_id: str,
        today: object,
    ) -> None:
        """Feed the EmotionAgent's anomaly verdict into the tracker.

        The EmotionAgent returns ``metadata["anomaly"]`` with
        ``{is_anomaly, z_score, direction}`` when the emotion
        was non-neutral. We forward the Z-score to the
        AnomalyTracker, which opens / closes the per-child
        anomaly event and (on close) observes the recovery
        latency into the North Star Prometheus histogram.

        We observe even when ``is_anomaly`` is False because the
        tracker needs the non-anomaly observation to close an
        open event. When the agent was neutral it returns
        ``anomaly=None`` and we skip the call (no Z-score to
        use as the close signal).

        Errors are logged but never raised so a tracker outage
        cannot break the chat reply.
        """
        emotion_output = outputs.get(AgentName.EMOTIONAL)
        if emotion_output is None:
            return
        anomaly_meta = (emotion_output.metadata or {}).get("anomaly")
        if anomaly_meta is None:
            return
        z_score = anomaly_meta.get("z_score")
        if z_score is None:
            return
        try:
            from niki.agents.anomaly_tracker import get_tracker

            tracker = get_tracker()
            tracker.observe(
                child_id=child_id,
                z_score=float(z_score),
                today=today,
            )
        except Exception:
            logger.exception("chat.anomaly_tracker_failed")

    async def _dispatch_tier_side_effects(
        self,
        *,
        tier_verdict: PrivacyTierVerdict,
        original_text: str,
        child_id: str | None,
        parent_id: str | None,
    ) -> bool:
        """Act on a privacy tier verdict.

        Wired into :meth:`chat` right after the tier is classified.
        Returns True iff a TREEHOLE_PRIVATE engagement succeeded (so
        the caller can record ``treehole_engaged`` on the
        ChatResult).

        Side-effects:

        * ``HIGH_RISK`` + child_id + parent_id → publish a
          SecurityAlert to the AlertBroker (real-time SSE push to
          the parent's dashboard) AND persist a DashboardAlert row
          via ``DashboardRepository.add_alert`` (history view).
        * ``TREEHOLE_PRIVATE`` + child_id + parent_id → engage the
          TreeholeManager so the original text is NEVER written to
          Chroma / Neo4j / conversation logs; only the AES-256-GCM
          encrypted emotion summary is stored (7-day TTL).

        All side-effect exceptions are caught + logged so a failing
        alert/treehole call never blocks the chat reply.
        """
        if child_id is None or parent_id is None:
            # HIGH_RISK with no child_id/parent_id means we can't
            # notify the parent — this is the production default
            # case in tests AND the production case until auth is
            # wired up. WARNING (not DEBUG) so it surfaces in any
            # non-debug log stream.
            logger.warning(
                "chat.tier_side_effects_skipped",
                tier=tier_verdict.tier.value,
                reason="missing child_id/parent_id",
            )
            return False

        # ---- HIGH_RISK: real-time parent alert + persisted row ----
        if tier_verdict.requires_parent_alert:
            try:
                from datetime import datetime, timezone

                from niki.realtime.alert_broker import get_alert_broker
                from niki.security.alerts import (
                    SEVERITY_CRITICAL,
                    SecurityAlert,
                )

                alert = SecurityAlert(
                    alert_type="high_risk_content",
                    severity=SEVERITY_CRITICAL,
                    actor_parent_id=parent_id,
                    target_parent_id=parent_id,
                    target_child_id=child_id,
                    client_ip=None,
                    event_count=1,
                    window_seconds=0,
                    detected_at=datetime.now(timezone.utc),
                    title="Niki detected high-risk content",
                    detail=(
                        f"Child message matched category "
                        f"{tier_verdict.matched_category!r}; "
                        f"please check in with your child."
                    ),
                )
                # BE-REALTIME-001: SSE pub/sub for the dashboard
                subscribers_notified = get_alert_broker().publish(alert)
                logger.info(
                    "chat.high_risk_alert_published",
                    child_id=child_id,
                    parent_id=parent_id,
                    subscribers=subscribers_notified,
                    category=tier_verdict.matched_category,
                )
            except Exception:
                # Use logger.exception so the traceback lands in
                # the log (narrow except in a follow-up if the
                # failure modes get classified).
                logger.exception(
                    "chat.high_risk_alert_publish_failed",
                )

            try:
                from niki.db.repositories.dashboard_repo import (
                    get_dashboard_repository,
                )

                repo = get_dashboard_repository()
                # Every implementation in the codebase is async;
                # we always await. (Removed the dead
                # `hasattr(result, "__await__")` check — it was
                # always True and obscured the real contract.)
                await repo.add_alert(
                    alert_type="high_risk_content",
                    child_id=child_id,
                    parent_id=parent_id,
                    payload={
                        "category": tier_verdict.matched_category,
                        "matched_words": tier_verdict.matched_words,
                        "reason": tier_verdict.reason,
                        "text_preview": original_text[:120],
                    },
                )
            except Exception:
                logger.exception(
                    "chat.high_risk_alert_persist_failed",
                )

        # ---- TREEHOLE_PRIVATE: encrypt + aggregate, no raw text ----
        treehole_engaged = False
        if tier_verdict.should_encrypt:
            try:
                from niki.treehole.manager import TreeholeManager

                manager = TreeholeManager()
                # The orchestrator only has the raw text. The
                # TreeholeManager will accept whatever emotion hint
                # we extract; until we have an LLM-side classifier
                # we default to "neutral" / 3 / "other", which
                # matches the "聚合即匿名" PRD #64 contract.
                _summary, _payload = await manager.engage(
                    child_id=child_id,
                    parent_id=parent_id,
                    original_text=original_text,
                    emotion="neutral",
                    intensity=3,
                    category="other",
                )
                treehole_engaged = True
                logger.info(
                    "chat.treehole_engaged",
                    child_id=child_id,
                    parent_id=parent_id,
                )
            except Exception:
                logger.exception(
                    "chat.treehole_engage_failed",
                )

        return treehole_engaged

    async def _update_age(self, age: int) -> None:
        """Reconfigure all agents for a new child age (thread-safe).

        ISS-12: the lock must cover the WHOLE reconfiguration, not
        just the top-level ``self.child_age`` write. Otherwise two
        concurrent calls can both pass the ``async with self._lock``
        block, then race on the sub-agent mutations and leave the
        output_gateway / per-agent child_age in a half-updated state.
        """
        async with self._lock:
            self.child_age = age
            self._output_gateway = OutputSafetyGateway(child_age=age)
            self._encyclopedia_agent.child_age = age
            self._encyclopedia_agent._age_group = self._encyclopedia_agent._resolve_age_group(age)
            self._tutoring_agent.child_age = age
            self._tutoring_agent._age_group = self._tutoring_agent._resolve_age_group(age)
            self._tutoring_agent._output_gateway = OutputSafetyGateway(child_age=age)
            self._emotion_agent.child_age = age
            self._emotion_agent._age_group = self._emotion_agent._resolve_age_group(age)
            self._emotion_agent._output_gateway = OutputSafetyGateway(child_age=age)

    async def _update_persona(self, persona: str) -> None:
        """Reconfigure all agents for a new persona (thread-safe).

        ISS-12: same rationale as _update_age — lock the whole
        reconfiguration, not just the top-level write.
        """
        async with self._lock:
            self.persona = persona
            self._persona_injector.set_default(Persona(persona))
            self._encyclopedia_agent.persona = persona
            self._tutoring_agent.persona = persona
            self._emotion_agent.persona = persona

    # ---- ISS-23: streaming variant ----

    async def stream_chat(
        self,
        text: str,
        *,
        session_id: str = "default",
        child_age: int | None = None,
        persona: str | None = None,
        child_id: str | None = None,
        parent_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming variant of :meth:`chat` (ISS-23).

        Yields dicts discriminated by ``kind``:

          * ``{"kind": "phase", "name": str, "payload": dict}`` —
            well-defined transitions (input_safety_ok / blocked /
            escalation, routed, agent_start/end, fused,
            output_safety_ok / block, answer_start/end, ...).
          * ``{"kind": "chunk", "text": str, "token_index": int}`` —
            codepoint chunks of the FINAL answer text. Emitted ONLY
            after ``strip_think_blocks`` + ``output_safety_gateway``
            have produced the sanitized text.
          * ``{"kind": "tool_call", "agent": str, "name": str,
            "args": dict}`` — LLM-initiated tool call (forwarded from
            the agent's ``on_phase``).
          * ``{"kind": "tool_result", "agent": str, "name": str,
            "ok": bool, "hit_count": int}`` — dispatcher result.
          * ``{"kind": "final", ...}`` — terminal event with full
            diagnostic metadata.

        The ``/chat/stream`` endpoint drains this generator and
        frames each dict as an SSE ``data: <json>\\n\\n`` line. The
        raw ``StreamingLLMProvider.stream(...)`` path stays in place
        for ``agent_stream_use_agent_pipeline=False``.
        """
        if child_age is not None and child_age != self.child_age:
            await self._update_age(child_age)
        if persona is not None and persona != self.persona:
            await self._update_persona(persona)

        stages: list[ChatStage] = []

        # 1. Input safety.
        input_verdict = await self._input_gateway.check(text)
        stages.append(ChatStage.SAFETY_INPUT)

        if input_verdict.is_blocked:
            # Even on block, classify tier so a high-risk signal
            # in a blocked message still fires the parent alert
            # and treehole is still engaged (defense in depth).
            tier_verdict = self._tier_classifier.classify(text)
            stages.append(ChatStage.TIER_CLASSIFY)
            treehole_engaged = await self._dispatch_tier_side_effects(
                tier_verdict=tier_verdict,
                original_text=text,
                child_id=child_id,
                parent_id=parent_id,
            )
            yield {
                "kind": "phase",
                "name": "input_blocked",
                "payload": {"reason": input_verdict.matched_category},
            }
            yield {
                "kind": "final",
                "session_id": session_id,
                "total_text": "嗯…这个问题Niki还没想好怎么回答呢~",
                "intent": None,
                "safety_alert": True,
                "parent_alert_required": tier_verdict.requires_parent_alert,
                "privacy_tier": tier_verdict.tier.value,
                "treehole_engaged": treehole_engaged,
                "is_final": True,
                "metadata": {
                    "input_action": input_verdict.action.value,
                    "input_category": input_verdict.matched_category,
                    "privacy_tier": tier_verdict.tier.value,
                    "privacy_tier_reason": tier_verdict.reason,
                    "child_age": self.child_age,
                    "persona": self.persona,
                },
            }
            return

        if input_verdict.is_escalation:
            caring_text = (
                "宝贝，Niki听到你说的话了。"
                "不管发生什么，Niki都在这里陪着你。"
                "我们一起想想办法，好不好？"
            )
            verdict = self._output_gateway.check(caring_text, is_tutoring_response=False)
            # Tier classification also runs on escalation so
            # TREEHOLE_PRIVATE children who also self-harm still
            # get the AES-256-GCM storage path (and the parent
            # alert goes out as usual).
            tier_verdict = self._tier_classifier.classify(text)
            stages.append(ChatStage.TIER_CLASSIFY)
            treehole_engaged = await self._dispatch_tier_side_effects(
                tier_verdict=tier_verdict,
                original_text=text,
                child_id=child_id,
                parent_id=parent_id,
            )
            yield {
                "kind": "phase",
                "name": "input_escalation",
                "payload": {"reason": input_verdict.matched_category},
            }
            # Caring text is short — emit as one chunk so the SSE
            # consumer sees the full reply.
            yield {
                "kind": "chunk",
                "text": verdict.sanitized_text or caring_text,
                "token_index": 0,
            }
            yield {
                "kind": "final",
                "session_id": session_id,
                "total_text": verdict.sanitized_text or caring_text,
                "intent": "emotional",
                "primary_agent": AgentName.EMOTIONAL.value,
                "safety_alert": True,
                "parent_alert_required": True,
                "privacy_tier": tier_verdict.tier.value,
                "treehole_engaged": treehole_engaged,
                "is_final": True,
            }
            return

        yield {
            "kind": "phase",
            "name": "input_safety_ok",
            "payload": {"category": input_verdict.matched_category},
        }

        # 1b. Privacy tier classification (#5). Mirrors :meth:`chat`:
        # HIGH_RISK fans out to parent alerts; TREEHOLE_PRIVATE
        # engages the AES-256-GCM storage path. Runs BEFORE intent
        # classification so the side-effects happen even when the
        # rest of the pipeline is short-circuited later.
        tier_verdict = self._tier_classifier.classify(text)
        stages.append(ChatStage.TIER_CLASSIFY)
        treehole_engaged = await self._dispatch_tier_side_effects(
            tier_verdict=tier_verdict,
            original_text=text,
            child_id=child_id,
            parent_id=parent_id,
        )

        # 2. Intent classification.
        intent_result = await self._classifier.classify(text)
        stages.append(ChatStage.CLASSIFY)

        # 3. Routing.
        routing_decision = self._router.route(intent_result)
        stages.append(ChatStage.ROUTE)
        yield {
            "kind": "phase",
            "name": "routed",
            "payload": {
                "intent": intent_result.primary.category.value if intent_result.primary else None,
                "primary": routing_decision.primary_agent.value,
                "parallel": [a.value for a in routing_decision.parallel_agents],
            },
        }

        # 4. Agent dispatch with streaming callbacks. We bridge the
        # sync ``yield`` boundary inside this generator to the
        # agent's ``await on_phase(...)`` coroutine calls via a
        # per-agent ``asyncio.Queue`` and a concurrent draining task.
        history = turns_to_history(await get_short_term_memory().get_recent(session_id))
        outputs: dict[AgentName, AgentOutput] = {}

        # BE-ISS-MEMORY-HOOK (streaming path): start the daytime
        # hook as a background task so the < 500ms extraction runs
        # in parallel with agent streaming. Skipped for
        # TREEHOLE_PRIVATE — same privacy contract as the
        # non-streaming path.
        if child_id is not None and tier_verdict.tier != PrivacyTier.TREEHOLE_PRIVATE:
            hook_task = asyncio.create_task(
                self._maybe_run_memory_hook(
                    child_id=child_id,
                    text=text,
                    context={
                        "intent": intent_result.primary.category.value
                        if intent_result.primary
                        else None
                    },
                )
            )
        else:
            hook_task = None

        for agent_name in routing_decision.all_agents:
            yield {
                "kind": "phase",
                "name": "agent_start",
                "payload": {"agent": agent_name.value},
            }

            # Per-agent event queue + counter.
            event_queue: asyncio.Queue[Any] = asyncio.Queue()
            token_box = [0]

            async def make_phase_cb(name_for_cb: str = agent_name.value) -> object:
                async def cb(name: str, payload: dict[str, Any]) -> None:
                    if name == "tool_call":
                        evt = {
                            "kind": "tool_call",
                            "agent": payload.get("agent", name_for_cb),
                            "name": payload.get("name", ""),
                            "args": payload.get("args", {}),
                        }
                    elif name == "tool_result":
                        evt = {
                            "kind": "tool_result",
                            "agent": payload.get("agent", name_for_cb),
                            "name": payload.get("name", ""),
                            "ok": bool(payload.get("ok", False)),
                            "hit_count": int(payload.get("hit_count", 0)),
                        }
                    else:
                        evt = {
                            "kind": "phase",
                            "name": name,
                            "payload": {
                                **payload,
                                "agent": payload.get("agent", name_for_cb),
                            },
                        }
                    await event_queue.put(evt)

                return cb

            async def make_chunk_cb() -> object:
                async def cb(text_chunk: str, idx: int) -> None:
                    await event_queue.put(
                        {
                            "kind": "chunk",
                            "text": text_chunk,
                            "token_index": token_box[0],
                        }
                    )
                    token_box[0] += 1

                return cb

            # Run the agent invocation concurrently with the queue
            # drain. The drain loop exits when the agent task ends.
            async def invoke_task() -> AgentOutput | None:
                try:
                    return await self._invoke_agent_streaming(
                        agent_name,
                        text,
                        session_id,
                        history,
                        phase_cb=await make_phase_cb(),
                        chunk_cb=await make_chunk_cb(),
                    )
                except Exception as exc:
                    logger.error(
                        "chat.stream.agent_failed",
                        agent=agent_name.value,
                        error=str(exc),
                    )
                    return None

            agent_task = asyncio.create_task(invoke_task())

            while True:
                # Wait for either a new event OR the agent to finish.
                get_task = asyncio.create_task(event_queue.get())
                done, _pending = await asyncio.wait(
                    {agent_task, get_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if get_task in done:
                    evt = get_task.result()
                    yield evt
                    # Continue draining until the agent task ends.
                    if agent_task.done():
                        # Flush remaining queue.
                        while not event_queue.empty():
                            yield event_queue.get_nowait()
                        break
                else:
                    # Agent task finished — cancel pending get and
                    # flush remaining events.
                    get_task.cancel()
                    try:
                        await get_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    while not event_queue.empty():
                        yield event_queue.get_nowait()
                    break

            out = agent_task.result()
            if out is not None:
                outputs[agent_name] = out

            yield {
                "kind": "phase",
                "name": "agent_end",
                "payload": {"agent": agent_name.value},
            }
        stages.append(ChatStage.DISPATCH)

        # BE-ISS-AUTOSAFE-VALVE (streaming): if the tutoring agent
        # triggered its safety valve during streaming, persist the
        # alert. Same contract as the non-streaming path.
        await self._maybe_record_safety_valve(
            outputs=outputs,
            child_id=child_id,
            parent_id=parent_id,
            problem_text=text,
        )

        # BE-ISS-MEMORY-HOOK (streaming): wait for the hook task we
        # kicked off before dispatch. If it failed, ``_maybe_run_
        # memory_hook`` already logged it; awaiting here just makes
        # sure the task is observed before the generator returns
        # (so an unhandled exception would surface).
        if hook_task is not None:
            try:
                await hook_task
            except Exception:
                logger.exception("chat.stream.memory_hook_failed")

        # BE-ISS-NORTHSTAR (streaming): same as the non-streaming
        # path — feed the emotion agent's anomaly verdict into the
        # tracker so the recovery-latency histogram is populated
        # for streamed sessions too.
        if tier_verdict.tier != PrivacyTier.TREEHOLE_PRIVATE and child_id is not None:
            from datetime import datetime, timezone as _tz

            self._track_emotion_anomaly(
                outputs=outputs,
                child_id=child_id,
                today=datetime.now(_tz.utc),
            )

        # 5. Fusion.
        fused = self._fusion.fuse(routing_decision, outputs)
        stages.append(ChatStage.FUSE)
        yield {
            "kind": "phase",
            "name": "fused",
            "payload": {
                "strategy": fused.strategy.value,
                "contributors": [a.value for a in fused.contributing_agents],
            },
        }

        # 6. Output safety.
        is_tutoring = routing_decision.primary_agent == AgentName.TUTORING
        output_verdict = self._output_gateway.check(fused.text, is_tutoring_response=is_tutoring)
        stages.append(ChatStage.SAFETY_OUTPUT)

        final_text = output_verdict.sanitized_text or fused.text
        safety_alert = output_verdict.action == OutputAction.BLOCK
        if safety_alert:
            final_text = "Niki觉得这个问题我们可以聊点别的呀~"
            yield {
                "kind": "phase",
                "name": "output_safety_block",
                "payload": {"reason": output_verdict.reason},
            }
        else:
            yield {
                "kind": "phase",
                "name": "output_safety_ok",
                "payload": {
                    "rewrite_count": len(output_verdict.rewrites_applied),
                },
            }

        # 7. Memory append.
        memory = get_short_term_memory()
        await memory.append(session_id, ConversationTurn(role="child", text=text))
        await memory.append(session_id, ConversationTurn(role="niki", text=final_text))
        stages.append(ChatStage.COMPLETE)

        # 8. Final event.
        yield {
            "kind": "final",
            "session_id": session_id,
            "total_text": final_text,
            "intent": intent_result.primary.category.value if intent_result.primary else None,
            "primary_agent": routing_decision.primary_agent.value,
            "parallel_agents": [a.value for a in routing_decision.parallel_agents],
            "is_composite": routing_decision.is_composite,
            "stages_completed": [s.value for s in stages],
            "safety_alert": safety_alert,
            "parent_alert_required": tier_verdict.requires_parent_alert,
            "privacy_tier": tier_verdict.tier.value,
            "privacy_tier_reason": tier_verdict.reason,
            "treehole_engaged": treehole_engaged,
            "metadata": {
                "input_action": input_verdict.action.value,
                "intent": intent_result.primary.category.value if intent_result.primary else None,
                "is_composite": routing_decision.is_composite,
                "fusion_strategy": fused.strategy.value,
                "contributing_agents": [a.value for a in fused.contributing_agents],
                "output_action": output_verdict.action.value,
                "privacy_tier": tier_verdict.tier.value,
                "privacy_tier_reason": tier_verdict.reason,
                "privacy_tier_category": tier_verdict.matched_category,
                "child_age": self.child_age,
                "persona": self.persona,
            },
            "is_final": True,
        }

    async def _invoke_agent_streaming(
        self,
        agent: AgentName,
        text: str,
        session_id: str,
        history: list[dict[str, str]],
        *,
        phase_cb: object,
        chunk_cb: Any,
    ) -> AgentOutput | None:
        """Invoke a single agent with streaming callbacks.

        Mirrors :meth:`_invoke_agent` but threads ``on_phase`` /
        ``on_chunk`` into agents that accept them (today: only
        EncyclopediaAgent). Tutoring / Emotion silently ignore the
        callbacks and return synchronously — which is fine for v1
        where only Encyclopedia meaningfully streams.
        """
        child_input = ChildInput(
            text=text,
            session_id=session_id,
            history=history,
        )  # type: ignore[call-arg]
        try:
            if agent == AgentName.ENCYCLOPEDIA:
                return await self._encyclopedia_agent.invoke(
                    child_input, on_phase=phase_cb, on_chunk=chunk_cb
                )
            elif agent == AgentName.TUTORING:
                return await self._tutoring_agent.invoke(child_input)
            elif agent == AgentName.EMOTIONAL:
                return await self._emotion_agent.invoke(child_input)
            else:
                logger.warning("chat.unknown_agent", agent=agent.value)
                return None
        except Exception as exc:
            logger.error("chat.invoke_failed", agent=agent.value, error=str(exc))
            return None
