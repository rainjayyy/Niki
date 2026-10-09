"""Chat orchestrator — the end-to-end pipeline that ties #1-#5 together.

Pipeline (PRD composite-intent flow):
  1. Input safety gateway (#1)
     - sensitive-word filter
     - topic-blacklist downgrade
     - self-harm escalation
  2. Intent classifier (#2) → top-level intent + tutoring subtype
  3. Router (#2) → primary + parallel agents (empathy-first ordering)
  4. Persona injector (#2) → fills {persona} in each agent's prompt
  5. Agent dispatch:
     - EncyclopediaAgent (#3) for encyclopedia
     - TutoringAgent (#4) for tutoring
     - EmotionAgent (#5) for emotional
  6. Fusion orchestrator (#2) → merge multi-agent outputs
  7. Output safety gateway (#1) → final fear-word rewrite, age check
  8. Return final text + diagnostics

This is the "vertical slice" that demonstrates ALL the previous slices
working together end-to-end.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable

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
from niki.schemas.common import AgentOutput, ChildInput
from niki.tools import ToolAccessChecker, ToolMatrix

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Stub LLMs (replace with real LLM clients in production)
# ---------------------------------------------------------------------------


class _StubDeepSeek:
    """Stub DeepSeek-V4 client for tests + dev.

    Returns canned text that contains the question so the safety
    gateway + fact-checker have something to work with.
    """

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 512,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Try to find the user's question and echo keywords
        user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "",
        )
        # Extract a short snippet as the canned response
        snippet = user_msg[:40] + "..." if len(user_msg) > 40 else user_msg or "Niki 陪着你~"
        return {"content": snippet}


class _StubDoubao:
    """Stub doubao for tests + dev (used by intent classifier)."""

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 256,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Would call the LLM for real; here we just return empty
        return {"content": '{"intents": []}'}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ChatStage(StrEnum):
    """Stages of the chat pipeline (for diagnostics + observability)."""

    SAFETY_INPUT = "safety_input"
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
        llm=None,
    ) -> None:
        self.child_age = child_age
        self.persona = persona

        # Safety (#1)
        self._input_gateway = InputSafetyGateway()
        self._output_gateway = OutputSafetyGateway(child_age=child_age)

        # Routing (#2) — use rule-based classifier (LLM stub returns empty)
        self._classifier = IntentClassifier(llm=None)
        self._router = AgentRouter()
        self._fusion = FusionOrchestrator()
        self._persona_injector = PersonaInjector(
            default=Persona(persona) if persona else Persona.GENTLE
        )

        # Knowledge (#3) + Agents (#3, #4, #5)
        self._kb = KnowledgeBase()
        self._fact_checker = FactChecker(self._kb)
        self._emotion_baseline = EmotionBaseline()
        # BE-AGENT-001: shared tool-permission checker for the
        # LLM-driven tool-use loop. Constructed per-orchestrator
        # instance so the audit log is scoped to a single client
        # lifecycle (matrix itself is read-only).
        self._tool_checker = ToolAccessChecker(ToolMatrix())
        # ISS-12: lock for thread-safe _update_age/_update_persona
        self._lock = asyncio.Lock()

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
    ) -> ChatResult:
        """Run the full chat pipeline.

        Args:
            text: The child's input text.
            session_id: Session identifier (for state continuity).
            child_age: Override the default child age.
            persona: Override the default persona.

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
            )

        if input_verdict.is_escalation:
            # Self-harm — produce a caring response and flag parent alert
            caring_text = (
                "宝贝，Niki听到你说的话了。"
                "不管发生什么，Niki都在这里陪着你。"
                "我们一起想想办法，好不好？"
            )
            verdict = self._output_gateway.check(caring_text, is_tutoring_response=False)
            return ChatResult(
                text=text,
                final_text=verdict.sanitized_text or caring_text,
                intent="emotional",
                primary_agent=AgentName.EMOTIONAL.value,
                safety_alert=True,
                parent_alert_required=True,
                stages_completed=stages + [ChatStage.SAFETY_OUTPUT, ChatStage.COMPLETE],
            )

        # 2. Intent classification (#2)
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
        }

        return ChatResult(
            text=text,
            final_text=final_text,
            intent=metadata["intent"],
            primary_agent=routing_decision.primary_agent.value,
            parallel_agents=[a.value for a in routing_decision.parallel_agents],
            is_composite=routing_decision.is_composite,
            stages_completed=stages,
            safety_rewrites=[{"from": o, "to": r} for o, r in output_verdict.rewrites_applied],
            parent_alert_required=False,
            metadata=metadata,
        )

    # ---- Internals ----

    async def _dispatch(
        self,
        decision,
        text: str,
        session_id: str,
        history: list[dict[str, str]],
    ) -> dict[AgentName, AgentOutput]:
        """Dispatch to each agent in the routing decision."""
        outputs: dict[AgentName, AgentOutput] = {}
        all_agents = decision.all_agents
        tasks = [self._invoke_agent(agent, text, session_id, history) for agent in all_agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for agent, result in zip(all_agents, results, strict=False):
            if isinstance(result, Exception):
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
                if result.text:
                    result.text = strip_think_blocks(result.text)
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
        )
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

    async def _update_age(self, age: int) -> None:
        """Reconfigure all agents for a new child age (thread-safe)."""
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
        """Reconfigure all agents for a new persona (thread-safe)."""
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
                "parent_alert_required": False,
                "is_final": True,
                "metadata": {"input_action": input_verdict.action.value},
            }
            return

        if input_verdict.is_escalation:
            caring_text = (
                "宝贝，Niki听到你说的话了。"
                "不管发生什么，Niki都在这里陪着你。"
                "我们一起想想办法，好不好？"
            )
            verdict = self._output_gateway.check(caring_text, is_tutoring_response=False)
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
                "is_final": True,
            }
            return

        yield {
            "kind": "phase",
            "name": "input_safety_ok",
            "payload": {"category": input_verdict.matched_category},
        }

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
        history = turns_to_history(
            await get_short_term_memory().get_recent(session_id)
        )
        outputs: dict[AgentName, AgentOutput] = {}

        for agent_name in routing_decision.all_agents:
            yield {
                "kind": "phase",
                "name": "agent_start",
                "payload": {"agent": agent_name.value},
            }

            # Per-agent event queue + counter.
            event_queue: asyncio.Queue[Any] = asyncio.Queue()
            token_box = [0]

            async def make_phase_cb(name_for_cb: str = agent_name.value):
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

            async def make_chunk_cb():
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
        output_verdict = self._output_gateway.check(
            fused.text, is_tutoring_response=is_tutoring
        )
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
        await memory.append(
            session_id, ConversationTurn(role="niki", text=final_text)
        )
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
            "parent_alert_required": False,
            "metadata": {
                "input_action": input_verdict.action.value,
                "intent": intent_result.primary.category.value if intent_result.primary else None,
                "is_composite": routing_decision.is_composite,
                "fusion_strategy": fused.strategy.value,
                "contributing_agents": [a.value for a in fused.contributing_agents],
                "output_action": output_verdict.action.value,
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
        phase_cb,
        chunk_cb,
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
        )
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
