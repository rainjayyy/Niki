"""Encyclopedia Agent — PRD '百科问答'.

End-to-end behaviour (PRD #1-5, #34, #43, #55, #56):
1. Receive a child's question (e.g. "为什么天是蓝的")
2. Retrieve from 3-level knowledge base (L1 core, L2 expanded, L3 live search for fact-check)
3. Compose an answer with DeepSeek-V4
4. Run fact-check middleware (target accuracy ≥ 95%)
5. Adapt language to child's age (3-5: ≤10 chars/sentence, 6-8: ≤15, 9-12: ≤20)
6. If unsure, admit "Niki 也不确定" (no fabrication — PRD #4)

The agent's base system prompt enforces:
- Never fabricate ("不知道就说不知道")
- Age-appropriate language
- Short, friendly sentences
- Use analogies ("像…一样")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import structlog

from niki.agents._loop_utils import (
    detect_repeat,
    estimate_tokens,
    truncate_messages,
)
from niki.agents.base import BaseAgent
from niki.config.settings import get_settings
from niki.knowledge.fact_checker import FactChecker, FactCheckResult
from niki.knowledge.knowledge_base import (
    KnowledgeBase,
    KnowledgeHit,
    KnowledgeSource,
)
from niki.memory.short_term import format_history
from niki.safety.output_gateway import (
    OutputAction,
    OutputSafetyGateway,
    strip_think_blocks,
)
from niki.schemas.common import AgentOutput, ChildInput
from niki.tools import (
    ToolAccessChecker,
    ToolName,
    agent_type_to_matrix_key,
    dispatch_tool_call,
    get_tool_schemas,
)

logger = structlog.get_logger(__name__)


# Honest-disclosure template (PRD #4)
UNSURE_RESPONSE_TEMPLATE = "嗯，Niki对这个也不太确定呢，我们一起去问问爸爸妈妈吧~"
IMAGE_UNCLEAR_TEMPLATE = "Niki看得不太清楚呢，能告诉Niki这是什么东西吗？"


@dataclass(frozen=True)
class EncyclopediaRequest:
    """Input shape for the Encyclopedia Agent."""

    question: str
    child_age: int = 7
    image_url: str | None = None
    image_caption: str | None = None  # pre-computed by vision model (if available)


@dataclass
class EncyclopediaResult:
    """Detailed result of an encyclopedia query (for observability)."""

    text: str
    sources_used: list[KnowledgeSource] = field(default_factory=list)
    hits: list[KnowledgeHit] = field(default_factory=list)
    fact_check: FactCheckResult | None = None
    was_rewritten: bool = False
    rewrite_count: int = 0
    age_group: str = "6-8"

    def to_agent_output(self) -> AgentOutput:
        return AgentOutput(
            text=self.text,
            emotion="neutral",
            intent="encyclopedia",
            metadata={
                "sources": [s.value for s in self.sources_used],
                "hit_count": len(self.hits),
                "fact_check_passed": (self.fact_check.is_confident if self.fact_check else None),
                "was_rewritten": self.was_rewritten,
                "age_group": self.age_group,
            },
        )


class EncyclopediaAgent(BaseAgent):
    """The encyclopedia / fact-answering agent.

    Inherits the persona + age adaptation scaffolding from BaseAgent.
    """

    agent_type = "encyclopedia"

    def __init__(
        self,
        llm: Any,
        knowledge_base: KnowledgeBase,
        fact_checker: FactChecker,
        persona: str = "gentle",
        child_age: int = 7,
        *,
        tool_checker: ToolAccessChecker | None = None,
    ) -> None:
        """Encyclopedia agent constructor.

        Args:
            llm: LLM client (BaseLLMClient) — must expose
                ``await chat(messages, ..., **kwargs)`` and return a
                dict with at least ``content``; tool_use responses
                also carry ``tool_calls`` (BE-AGENT-001).
            knowledge_base: The shared KnowledgeBase instance.
            fact_checker: The shared FactChecker instance.
            persona: One of ``"lively"`` / ``"gentle"`` / ``"rigorous"``.
            child_age: 3-12 inclusive; drives age-group prompt.
            tool_checker: Optional permission checker for the
                LLM-driven tool-use loop (BE-AGENT-001). When
                ``None`` the legacy "1 LLM call" path is always
                used, regardless of the
                ``agent_tool_use_enabled`` setting. The orchestrator
                and the API module inject the shared checker; tests
                that want loop coverage must pass an explicit one.
        """
        super().__init__(llm, persona, child_age)
        self._kb = knowledge_base
        self._fact_checker = fact_checker
        self._output_gateway = OutputSafetyGateway(child_age=child_age)
        self._tool_checker = tool_checker

    def _build_system_prompt(self) -> str:
        """Build the system prompt for the encyclopedia agent.

        Includes (PRD):
        - Honest disclosure rule (#4)
        - No fabricated answers
        - Age-appropriate language (#34, #56)
        - Use analogies
        - No fear-invoking words (#5)
        - Never ask for private info (#42)
        """
        return (
            "你是一个知识渊博且诚实的AI伙伴，名叫Niki。\n\n"
            "【核心规则】\n"
            "1. 不知道就诚实说「Niki也不确定」——绝不能编造答案。\n"
            "2. 用简单、生动的比喻来解释复杂概念。\n"
            "3. 适龄语言：每句话不超过指定字数（按年龄段）。\n"
            "4. 避免恐惧词汇（死亡、怪物、灾难等）——用更温柔的词替代。\n"
            "5a. 严禁改写孩子输入中的暴力/自伤/隐私词。如果孩子说「杀」或「打」，\n"
            "    你必须直接表达拒绝/转移话题，绝不能把「杀」解释成「离开」、「打」解释成「拍」。\n"
            "5. 绝不要向孩子询问住址、电话等隐私信息。\n"
            "6. 答案要简短有趣，不要长篇大论。\n"
            "7. 根据下方知识库检索到的内容回答。\n\n"
            f"【年龄适配】孩子年龄 {self.child_age} 岁 ({self._age_group})：\n"
            f"{self.get_age_guardrails()}"
        )

    async def _process(
        self,
        child_input: ChildInput,
        context: dict[str, Any] | None = None,
        *,
        on_phase: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_chunk: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        """Process an encyclopedia question end-to-end.

        Args:
            child_input: The child's question + metadata.
            context: Optional pre-fetched hits / hints (skips KB query).
            on_phase: ISS-23 streaming hook — coroutine called at
                well-defined loop transitions. ``None`` = silent.
            on_chunk: ISS-23 streaming hook — coroutine called with
                ``(text, idx)`` codepoint chunks of the FINAL answer.
                ``None`` = silent.

        Returns:
            AgentOutput with the answer.
        """
        question = (child_input.text or "").strip()
        if not question and not child_input.image_url:
            return AgentOutput(
                text="嗯，Niki没听清问题呢，能再说一次吗？",
                emotion="neutral",
                intent="encyclopedia",
            )

        # BE-AGENT-001: opt-in to the LLM-driven tool-use loop.
        # The gate requires BOTH the settings flag and a non-None
        # checker — tests that build agents without a checker fall
        # through to the legacy path automatically, regardless of
        # the flag value. This is the explicit, observable branch
        # point.
        settings = get_settings()
        if settings.agent_tool_use_enabled and self._tool_checker is not None:
            return await self._process_with_tool_loop(
                child_input, context, on_phase=on_phase, on_chunk=on_chunk
            )

        # 1. Resolve the actual question (from image caption if needed)
        actual_question = self._resolve_question(question, child_input)

        logger.info(
            "encyclopedia.process",
            question=actual_question[:100],
            child_age=self.child_age,
            has_image=bool(child_input.image_url),
        )

        # 2. Retrieve from KB
        hits = self._retrieve(actual_question, child_input, context)

        RELEVANCE_THRESHOLD = 0.15
        relevant_hits = [h for h in hits if h.score >= RELEVANCE_THRESHOLD]
        retrieval_status = (
            "no_hits" if not hits else "low_relevance" if not relevant_hits else "kb_hits"
        )
        hits = relevant_hits

        # No knowledge and no conversation history → honest disclosure.
        # With history we still ask the LLM so follow-up questions like
        # "我刚才问的是什么？" can be answered from the session context.
        if not hits and not child_input.history:
            return AgentOutput(
                text=UNSURE_RESPONSE_TEMPLATE,
                emotion="neutral",
                intent="encyclopedia",
                metadata={
                    "retrieval_status": retrieval_status,
                    "question": actual_question,
                    "age_group": self._age_group,
                    "sources": [],
                },
            )

        # 3. Compose the answer via LLM
        if hits:
            context_text = self._format_hits(hits)
            user_content = f"问题：{actual_question}\n\n参考资料：\n{context_text}"
        else:
            context_text = ""
            user_content = (
                f"问题：{actual_question}\n\n"
                "知识库里没有查到相关参考。请根据上方对话上下文回答；"
                "如果确实不知道，就诚实告诉孩子你也不确定。"
            )
        messages = [
            {"role": "system", "content": self.get_system_prompt_with_persona()},
            *format_history(child_input.history),
            {"role": "user", "content": user_content},
        ]

        try:
            response = await self.llm.chat(
                messages=messages,
                temperature=0.5,
                max_tokens=512,
            )
        except Exception as exc:
            logger.error("encyclopedia.llm_failed", error=str(exc))
            return AgentOutput(
                text=UNSURE_RESPONSE_TEMPLATE,
                emotion="neutral",
                intent="encyclopedia",
            )

        draft = response["content"]

        # 4. Fact-check the draft against KB
        if hits:
            fact_check = await self._fact_checker.check(
                claim=draft,
                evidence=[h.content for h in hits],
                use_live_search=False,  # Live search only when KB is empty (handled in checker)
            )
            if not fact_check.is_confident:
                logger.info(
                    "encyclopedia.fact_check_failed",
                    confidence=fact_check.confidence,
                    reason=fact_check.reason,
                )
                return AgentOutput(
                    text=UNSURE_RESPONSE_TEMPLATE,
                    emotion="neutral",
                    intent="encyclopedia",
                    metadata={
                        "fact_check_failed": True,
                        "fact_check_reason": fact_check.reason,
                        # Always include sources for consistent shape
                        "sources": list({h.source.value for h in hits}),
                    },
                )
        else:
            # No KB evidence to verify against; the contextual answer
            # still passes through the output safety gateway below.
            fact_check = FactCheckResult(
                is_confident=True,
                confidence=1.0,
                reason="contextual_answer_no_kb_evidence",
                supporting_sources=[],
            )

        # 5. Run output safety gateway (fear-word rewrite, privacy, age checks)
        verdict = self._output_gateway.check(draft, is_tutoring_response=False)
        final_text = verdict.sanitized_text or draft

        # 6. Build the result
        result = EncyclopediaResult(
            text=final_text,
            sources_used=list({h.source for h in hits}),
            hits=hits,
            fact_check=fact_check,
            was_rewritten=bool(verdict.rewrites_applied),
            rewrite_count=len(verdict.rewrites_applied),
            age_group=self._age_group,
        )

        logger.info(
            "encyclopedia.complete",
            sources=[s.value for s in result.sources_used],
            hit_count=len(result.hits),
            rewrite_count=result.rewrite_count,
            text_length=len(result.text),
        )

        return result.to_agent_output()

    # ---- BE-AGENT-001: LLM-driven tool-use loop ----

    # 1-round ReAct default; ISS-22 lifts this to a bounded loop
    # controlled by ``agent_tool_loop_max_rounds`` etc. ``_run_react_loop``
    # is the bounded-loop engine; ``_process_with_tool_loop`` is now a
    # thin wrapper that just reads settings and delegates.
    _ALLOWED_TOOL_NAMES = frozenset({"knowledge_base.search"})

    # Map the wire-format tool name the LLM emits to the
    # ``ToolName`` enum the permission matrix understands. Returns
    # ``None`` for unknown names so the caller can skip the matrix
    # check and fall through to the dispatcher's "unknown tool"
    # path (which returns an error envelope — never a crash).
    _TOOL_NAME_TO_ENUM: dict[str, str] = {
        "knowledge_base.search": "knowledge_base",
        "time_query.now": "time_query",
        "image_recognition.caption": "image_recognition",
        "search_api.query": "search_api",
        "weather_query.now": "weather_query",
    }

    @classmethod
    def _resolve_tool_enum(cls, tool_name: str) -> Any | None:
        """Resolve a wire-format tool name to its ``ToolName`` enum.

        Returns ``None`` for names that aren't in the dispatcher's
        known set — the caller should let the dispatcher emit its
        "unknown tool" error envelope instead of trying to
        check the matrix with a bogus enum value.
        """
        from niki.tools.matrix import ToolName

        enum_value = cls._TOOL_NAME_TO_ENUM.get(tool_name)
        if enum_value is None:
            return None
        try:
            return ToolName(enum_value)
        except ValueError:
            return None

    async def _process_with_tool_loop(
        self,
        child_input: ChildInput,
        context: dict[str, Any] | None = None,
        *,
        on_phase: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_chunk: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        """LLM-driven tool-use path (BE-AGENT-001 + ISS-22).

        Thin wrapper around :meth:`_run_react_loop` that reads the
        loop's bounds from settings. Behavior matches the legacy
        1-round path exactly when
        ``agent_tool_loop_max_rounds == 1``.

        See :meth:`_run_react_loop` for the loop's termination
        criteria and round accounting.
        """
        settings = get_settings()
        return await self._run_react_loop(
            child_input,
            context,
            max_rounds=settings.agent_tool_loop_max_rounds,
            max_tokens=settings.agent_tool_loop_max_tokens,
            max_tool_calls=settings.agent_tool_loop_max_tool_calls,
            on_phase=on_phase,
            on_chunk=on_chunk,
        )

    async def _run_react_loop(
        self,
        child_input: ChildInput,
        context: dict[str, Any] | None,
        *,
        max_rounds: int,
        max_tokens: int,
        max_tool_calls: int,
        on_phase: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_chunk: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        """Bounded ReAct tool-use loop (ISS-22 + ISS-23 streaming seam).

        Runs up to ``max_rounds`` LLM turns. Each round:

          1. Token-budget gate — drop oldest round(s) if the message
             list exceeds ``max_tokens`` (cheap char estimate).
          2. Call LLM with the tool schemas + ``tool_choice="auto"``.
          3. If the response has no ``tool_calls``, treat its
             ``content`` as the final answer (Termination 1).
          4. If ``content`` is an honest-disclosure signal (e.g. "Niki
             也不确定"), accept it as the answer (Termination 2).
          5. Otherwise dispatch the tool calls through the matrix +
             whitelist, append the tool results, and loop.

        Other termination criteria — see comments inline.

        Args:
            child_input: Same as ``_process``.
            context: Same as ``_process``.
            max_rounds: Hard ceiling on the loop iterations. ``1``
                reproduces the BE-AGENT-001 1-round baseline.
            max_tokens: Per-loop token budget; when exceeded,
                ``truncate_messages`` drops oldest rounds.
            max_tool_calls: Cap on total tool invocations across all
                rounds (prevents runaway when the LLM emits a fan-out
                of calls per round).
            on_phase: Optional coroutine fired at every loop transition.
                ``None`` = silent (the legacy default).
            on_chunk: Optional coroutine fired with codepoint chunks of
                the FINAL answer text (after output safety). ``None``
                = silent.

        Returns:
            The final :class:`AgentOutput` after the
            fact-check + output-safety pipeline.
        """

        async def _phase(name: str, payload: dict[str, Any]) -> None:
            """Defensive wrapper around ``on_phase`` — caller bugs don't crash the agent."""
            if on_phase is None:
                return
            try:
                await on_phase(name, payload)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "encyclopedia.streaming.phase_failed",
                    name=name,
                    error=str(exc),
                )

        # Module-level callables used after the loop to fake-stream the
        # final answer text into codepoint chunks. The chunk size mirrors
        # the existing mock streaming provider; replace with real
        # ``llm.chat_stream`` delta assembly in ISS-25.
        async def _chunk(text: str, idx: int) -> None:
            if on_chunk is None:
                return
            try:
                await on_chunk(text, idx)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "encyclopedia.streaming.chunk_failed",
                    idx=idx,
                    error=str(exc),
                )

        # 1. Initial messages.
        system_prompt = self.get_system_prompt_with_persona()
        history = list(child_input.history or [])
        user_msg = child_input.text or ""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *format_history(history),
            {"role": "user", "content": user_msg},
        ]

        tool_schemas = get_tool_schemas(
            self.agent_type,
            checker=self._tool_checker,
        )
        matrix_key = agent_type_to_matrix_key(self.agent_type)

        kb_hits: list[KnowledgeHit] = []
        tool_call_history: list[tuple[str, str]] = []
        consecutive_error_envelopes = 0
        round_idx = 0
        last_response: dict[str, Any] | None = None
        final_draft = ""

        # Tracks whether the last LLM turn was a no-tools compose, so
        # we don't burn an extra LLM call at the tail when the loop
        # already produced an answer.
        last_was_no_tool = False

        while round_idx < max_rounds:
            # Token-budget gate BEFORE the LLM call — avoid generating
            # tokens we can't afford to feed back next round.
            if estimate_tokens(messages) > max_tokens:
                messages = truncate_messages(
                    messages,
                    soft_cap_chars=max_tokens * 4,
                )

            # ISS-23: announce we're about to think. The ``round`` field
            # disambiguates events when the LLM takes multiple rounds.
            await _phase("thinking_start", {"round": round_idx, "agent": "encyclopedia"})

            # LLM call.
            try:
                response = await self.llm.chat(
                    messages=messages,
                    tools=tool_schemas,
                    tool_choice="auto",
                    temperature=0.5,
                    max_tokens=512,
                )
            except Exception as exc:
                logger.error(
                    "encyclopedia.react.llm_failed",
                    round=round_idx,
                    error=str(exc),
                )
                await _phase(
                    "thinking_end",
                    {"round": round_idx, "agent": "encyclopedia", "finish_reason": "error"},
                )
                return self._fallback_output(
                    UNSURE_RESPONSE_TEMPLATE,
                    tool_loop_used=True,
                    error=f"llm_failed_round_{round_idx}",
                )

            last_response = response
            tool_calls = response.get("tool_calls") or []
            await _phase(
                "thinking_end",
                {
                    "round": round_idx,
                    "agent": "encyclopedia",
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                },
            )

            # Termination 1: LLM chose no tool → its content is the answer.
            if not tool_calls:
                # Even on the no-tool branch, re-retrieve KB server-side
                # so the fact-checker has evidence to ground against.
                final_draft = response.get("content") or ""
                hits = self._retrieve(user_msg, child_input, context)
                RELEVANCE_THRESHOLD = 0.15
                relevant_hits = [h for h in hits if h.score >= RELEVANCE_THRESHOLD]
                kb_hits.extend(relevant_hits)
                last_was_no_tool = True
                break

            # Termination 2: "I don't know" honest disclosure signal.
            content = response.get("content") or ""
            if "Niki也不确定" in content or "不确定" in content:
                final_draft = content or UNSURE_RESPONSE_TEMPLATE
                last_was_no_tool = True
                break

            # Termination 3: total tool-call budget exhausted.
            new_pairs = [
                (
                    tc.get("function", {}).get("name", ""),
                    str(tc.get("function", {}).get("arguments", "")),
                )
                for tc in tool_calls
            ]
            if len(tool_call_history) + len(new_pairs) > max_tool_calls:
                logger.warning(
                    "encyclopedia.react.tool_budget_exceeded",
                    round=round_idx,
                    tool_budget=max_tool_calls,
                )
                # Use whatever content the LLM had on this round.
                final_draft = content
                last_was_no_tool = True
                break

            # Termination 4: repeated tool call (LLM looping).
            if any(detect_repeat(tool_call_history, p) for p in new_pairs):
                logger.warning(
                    "encyclopedia.react.repeat_break",
                    round=round_idx,
                )
                final_draft = content
                last_was_no_tool = True
                break
            tool_call_history.extend(new_pairs)

            # Dispatch the calls.
            (
                appended_messages,
                new_kb_hits,
                any_allowed,
                all_errors,
            ) = await self._dispatch_tool_calls(
                tool_calls,
                matrix_key,
                kb=self._kb,
                image_caption=getattr(child_input, "image_caption", None),
                on_phase=on_phase,
            )
            kb_hits.extend(new_kb_hits)

            # Termination 5: all rejections (whitelist / matrix).
            if not any_allowed:
                return AgentOutput(
                    text=UNSURE_RESPONSE_TEMPLATE,
                    emotion="neutral",
                    intent="encyclopedia",
                    metadata={
                        "tool_loop_used": True,
                        "tool_loop_rounds": round_idx,
                        "tool_calls_rejected": [
                            tc.get("function", {}).get("name", "") for tc in tool_calls
                        ],
                        "age_group": self._age_group,
                        "sources": [],
                    },
                )

            # Termination 6: 2 consecutive tool-error envelopes — give up.
            if all_errors:
                consecutive_error_envelopes += 1
                if consecutive_error_envelopes >= 2:
                    logger.warning(
                        "encyclopedia.react.error_envelope_break",
                        round=round_idx,
                    )
                    final_draft = content
                    last_was_no_tool = True
                    break
            else:
                consecutive_error_envelopes = 0

            # Build next-round messages. OpenAI spec invariant:
            # assistant-with-tool_calls must immediately precede
            # role:tool messages.
            messages = (
                messages
                + [
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls,
                    },
                ]
                + appended_messages
            )
            round_idx += 1

        # Termination 7: max rounds exhausted (we ran out of budget).
        # If the last LLM turn had no tool_calls, final_draft was set
        # in the loop body. Otherwise we need a final compose call so
        # the agent isn't silent.
        if not final_draft and last_response is not None:
            final_draft = last_response.get("content") or ""

        if round_idx >= max_rounds and not last_was_no_tool and last_response is not None:
            try:
                final = await self.llm.chat(
                    messages=messages,
                    temperature=0.5,
                    max_tokens=512,
                )
                final_draft = final.get("content") or ""
            except Exception:
                final_draft = ""

        # Dedup KB hits by their source id so re-querying the same
        # entry twice doesn't double-count in the final sources list.
        deduped_hits = self._dedup_hits(kb_hits)

        # Hand off to the shared output pipeline.
        result = await self._finalise(
            final_draft,
            sources=deduped_hits,
            tool_loop_used=True,
            fact_check_evidence=[h.content for h in deduped_hits],
        )
        # Surface round count for observability.
        result.metadata["tool_loop_rounds"] = round_idx

        # ISS-23: fake-stream the final sanitized answer as codepoint
        # chunks. Real ``llm.chat_stream`` with tool_use delta assembly
        # is ISS-25. We stream ONLY AFTER ``_finalise`` (which has
        # already run strip_think_blocks + fact-check + output
        # safety), so the child never sees a leaked reasoning block
        # or a partial-corrected string.
        await _phase("answer_start", {"agent": "encyclopedia"})
        if on_chunk is not None and result.text:
            chunk_size = 4  # codepoints per chunk
            text = result.text
            for idx in range(0, len(text), chunk_size):
                await _chunk(text[idx : idx + chunk_size], idx)
        await _phase("answer_end", {"agent": "encyclopedia"})

        return result

    async def _dispatch_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        matrix_key: Any,
        *,
        kb: KnowledgeBase,
        image_caption: str | None,
        on_phase: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[KnowledgeHit], bool, bool]:
        """Dispatch one round's worth of tool calls.

        Returns:
            ``(appended_messages, kb_hits, any_allowed, all_errors)``.

            * ``appended_messages`` — OpenAI role:tool messages to
              feed back to the LLM next round.
            * ``kb_hits`` — re-hydrated :class:`KnowledgeHit` objects
              for the ``sources`` metadata.
            * ``any_allowed`` — True if at least one call was
              whitelisted + matrix-allowed (the caller uses this for
              Termination 5).
            * ``all_errors`` — True if every dispatched call returned
              a JSON error envelope (the caller uses this for
              Termination 6, the 2-consecutive-error break).

        Note: ``_run_react_loop`` calls this once per round and
        accumulates the KB hits across rounds.

        ISS-23: ``on_phase`` fires ``tool_call`` before each dispatch
        and ``tool_result`` after. Wrapped in defensive try/except so
        a misbehaving consumer cannot crash the dispatch.
        """
        appended_messages: list[dict[str, Any]] = []
        kb_hits: list[KnowledgeHit] = []
        any_allowed = False
        any_non_error = False
        all_errors = True

        async def _phase(name: str, payload: dict[str, Any]) -> None:
            if on_phase is None:
                return
            try:
                await on_phase(name, payload)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "encyclopedia.streaming.phase_failed",
                    name=name,
                    error=str(exc),
                )

        for call in tool_calls:
            fn = call.get("function") or {}
            tool_name = fn.get("name", "")
            # Parse args for the streaming payload (best-effort —
            # dispatcher will re-validate). Bad JSON here is fine; we
            # just send an empty ``args`` payload.
            try:
                args_obj = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args_obj = {}

            if tool_name not in self._ALLOWED_TOOL_NAMES:
                logger.warning(
                    "encyclopedia.tool_loop.tool_not_whitelisted",
                    tool=tool_name,
                    agent=matrix_key,
                )
                continue

            # Stream the call BEFORE dispatch so consumers see the
            # intent even if the dispatcher is slow.
            await _phase(
                "tool_call",
                {"agent": "encyclopedia", "name": tool_name, "args": args_obj},
            )

            # Sandbox enforcement: resolve the wire-format tool name
            # to its ``ToolName`` enum, consult the permission matrix,
            # and skip the dispatch when the matrix denies the call.
            # The streaming ``tool_call`` event is still emitted so
            # the dashboard shows the attempt, and a sibling
            # ``tool_result`` with ``ok=False`` is fired so consumers
            # see the rejection in the SSE stream.
            tool_enum = self._resolve_tool_enum(tool_name)
            if tool_enum is not None:
                access = self._tool_checker.check(matrix_key, tool_enum)
                if not access.allowed:
                    logger.warning(
                        "encyclopedia.tool_loop.matrix_denied",
                        tool=tool_name,
                        agent=matrix_key,
                        reason=access.reason,
                    )
                    appended_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": json.dumps(
                                {
                                    "error": "permission_denied",
                                    "tool": tool_name,
                                    "reason": access.reason,
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                    all_errors = all_errors and True
                    await _phase(
                        "tool_result",
                        {
                            "agent": "encyclopedia",
                            "name": tool_name,
                            "ok": False,
                            "hit_count": 0,
                        },
                    )
                    continue
            any_allowed = True

            result_str = await dispatch_tool_call(
                call,
                kb=kb,
                image_caption=image_caption,
            )
            appended_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result_str,
                }
            )

            # Track error envelopes — dispatcher returns ``{"error":
            # "..."}`` for malformed args; the rest is JSON list of
            # hits. Used by Termination 6 to break the loop after
            # 2 consecutive error-only rounds.
            is_error_envelope = False
            hit_count_for_event = 0
            if ToolName.KNOWLEDGE_BASE.value in tool_name:
                try:
                    parsed = json.loads(result_str)
                    if isinstance(parsed, dict) and "error" in parsed:
                        is_error_envelope = True
                    elif isinstance(parsed, list):
                        any_non_error = True
                        all_errors = False
                        hit_count_for_event = len(parsed)
                        for item in parsed:
                            if not isinstance(item, dict):
                                continue
                            try:
                                src_enum = KnowledgeSource(item.get("source", "l1_core"))
                            except ValueError:
                                src_enum = KnowledgeSource.L1_CORE
                            kb_hits.append(
                                KnowledgeHit(
                                    content=item.get("content", ""),
                                    source=src_enum,
                                    score=float(item.get("score", 0.0)),
                                    metadata={"id": item.get("id")},
                                )
                            )
                except (json.JSONDecodeError, ValueError, TypeError):
                    is_error_envelope = True
            else:
                # Non-KB whitelisted tool (none today, reserved).
                any_non_error = True
                all_errors = False

            if not is_error_envelope and not any_non_error:
                # Defensive — if a non-error JSON arrived but didn't
                # produce a hit (e.g. KB empty), don't lock the loop
                # into an all-error chain. Treat empty KB as success.
                all_errors = False

            await _phase(
                "tool_result",
                {
                    "agent": "encyclopedia",
                    "name": tool_name,
                    "ok": not is_error_envelope,
                    "hit_count": hit_count_for_event,
                },
            )

        if any_allowed and any_non_error:
            all_errors = False
        elif any_allowed and not any_non_error and not kb_hits:
            # KB returned [] (empty result, not an error). Don't punish
            # the loop for that — set all_errors=False so we don't
            # break early just because the KB has no entry for the
            # current query.
            all_errors = False

        return appended_messages, kb_hits, any_allowed, all_errors

    @staticmethod
    def _dedup_hits(hits: list[KnowledgeHit]) -> list[KnowledgeHit]:
        """Deduplicate ``KnowledgeHit`` objects by ``metadata["id"]``.

        The LLM may legitimately re-query the same KB entry across
        rounds with slightly different wording (e.g. round 0
        "sky blue" → round 1 "瑞利散射"). We don't want that entry
        counted twice in the final ``sources`` metadata.
        """
        seen: set[str] = set()
        out: list[KnowledgeHit] = []
        for h in hits:
            key = str((h.metadata or {}).get("id") or h.content[:64])
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
        return out

    async def _finalise(
        self,
        draft: str,
        *,
        sources: list[KnowledgeHit],
        tool_loop_used: bool,
        fact_check_evidence: list[str] | None = None,
    ) -> AgentOutput:
        """Shared tail of the legacy and tool-loop paths.

        Runs ``strip_think_blocks`` (defense in depth for reasoning
        models), the fact-checker, and the output-safety gateway.
        Returns the final :class:`AgentOutput` plus a small
        ``tool_loop_used`` flag for observability.
        """
        # Strip reasoning blocks the LLM leaked (M3 / R1 do this).
        draft = strip_think_blocks(draft or "")

        # Fact-check against any KB evidence we collected.
        fact_check: FactCheckResult | None = None
        if fact_check_evidence:
            fact_check = await self._fact_checker.check(
                claim=draft,
                evidence=fact_check_evidence,
                use_live_search=False,
            )
            if not fact_check.is_confident:
                logger.info(
                    "encyclopedia.tool_loop.fact_check_failed",
                    confidence=fact_check.confidence,
                    reason=fact_check.reason,
                )
                return AgentOutput(
                    text=UNSURE_RESPONSE_TEMPLATE,
                    emotion="neutral",
                    intent="encyclopedia",
                    metadata={
                        "tool_loop_used": tool_loop_used,
                        "fact_check_failed": True,
                        "fact_check_reason": fact_check.reason,
                        "age_group": self._age_group,
                        "sources": list({h.source.value for h in sources}),
                    },
                )

        # Output safety — fear-word rewrite, privacy, age check.
        verdict = self._output_gateway.check(draft, is_tutoring_response=False)
        final_text = verdict.sanitized_text or draft

        if verdict.action == OutputAction.BLOCK:
            return AgentOutput(
                text="Niki觉得这个问题我们可以聊点别的呀~",
                emotion="neutral",
                intent="encyclopedia",
                metadata={
                    "tool_loop_used": tool_loop_used,
                    "age_group": self._age_group,
                    "sources": list({h.source.value for h in sources}),
                    "output_blocked": True,
                },
            )

        logger.info(
            "encyclopedia.tool_loop.complete",
            tool_loop_used=tool_loop_used,
            hit_count=len(sources),
            rewrite_count=len(verdict.rewrites_applied),
            text_length=len(final_text),
        )

        return AgentOutput(
            text=final_text,
            emotion="neutral",
            intent="encyclopedia",
            metadata={
                "tool_loop_used": tool_loop_used,
                "sources": list({h.source.value for h in sources}),
                "hit_count": len(sources),
                "was_rewritten": bool(verdict.rewrites_applied),
                "rewrite_count": len(verdict.rewrites_applied),
                "age_group": self._age_group,
            },
        )

    def _fallback_output(
        self,
        text: str,
        *,
        tool_loop_used: bool,
        error: str,
    ) -> AgentOutput:
        """Construct a safe fallback :class:`AgentOutput` on hard error."""
        return AgentOutput(
            text=text,
            emotion="neutral",
            intent="encyclopedia",
            metadata={
                "tool_loop_used": tool_loop_used,
                "error": error,
                "age_group": self._age_group,
            },
        )

    @staticmethod
    def _resolve_question(text: str, child_input: ChildInput) -> str:
        """If the child shared an image, use the caption as the question."""
        if not text and child_input.image_url:
            return "请描述这张图片"  # Fallback
        return text

    def _retrieve(
        self,
        question: str,
        child_input: ChildInput,
        context: dict[str, Any] | None,
    ) -> list[KnowledgeHit]:
        """Retrieve from the knowledge base.

        If the context has pre-fetched hits, use them. Otherwise query the KB.
        """
        if context and "hits" in context:
            cached = context["hits"]
            if isinstance(cached, list):
                return [h for h in cached if isinstance(h, KnowledgeHit)]

        # Query KB
        top_k = 3
        if self._age_group == "3-5":
            top_k = 2  # Younger children get fewer sources to keep answer short
        return self._kb.search(question, top_k=top_k)

    @staticmethod
    def _format_hits(hits: list[KnowledgeHit]) -> str:
        """Format KB hits as LLM context."""
        if not hits:
            return ""
        lines = []
        for idx, hit in enumerate(hits, start=1):
            lines.append(f"[{idx}] 来源: {hit.source.value} (相关度: {hit.score:.2f})")
            lines.append(f"    {hit.content}")
        return "\n".join(lines)

    # ---- Convenience entry points ----

    async def ask(self, request: EncyclopediaRequest) -> AgentOutput:
        """Convenience method: ask an encyclopedia question."""
        child_input = ChildInput(
            text=request.question,
            session_id="encyclopedia",
            image_url=request.image_url,
        )  # type: ignore[call-arg]
        return await self.invoke(child_input)
