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

from dataclasses import dataclass, field
from typing import Any

import structlog

from niki.agents.base import BaseAgent
from niki.knowledge.fact_checker import FactChecker, FactCheckResult
from niki.knowledge.knowledge_base import (
    KnowledgeBase,
    KnowledgeHit,
    KnowledgeSource,
)
from niki.safety.output_gateway import OutputSafetyGateway
from niki.schemas.common import AgentOutput, ChildInput

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
    image_caption: str | None = None   # pre-computed by vision model (if available)


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
                "fact_check_passed": (
                    self.fact_check.is_confident if self.fact_check else None
                ),
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
        llm,
        knowledge_base: KnowledgeBase,
        fact_checker: FactChecker,
        persona: str = "gentle",
        child_age: int = 7,
    ) -> None:
        super().__init__(llm, persona, child_age)
        self._kb = knowledge_base
        self._fact_checker = fact_checker
        self._output_gateway = OutputSafetyGateway(child_age=child_age)

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
    ) -> AgentOutput:
        """Process an encyclopedia question end-to-end.

        Args:
            child_input: The child's question + metadata.
            context: Optional pre-fetched hits / hints (skips KB query).

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

        if not hits:
            # No knowledge → honest disclosure
            return AgentOutput(
                text=UNSURE_RESPONSE_TEMPLATE,
                emotion="neutral",
                intent="encyclopedia",
                metadata={
                    "retrieval_status": "no_hits",
                    "question": actual_question,
                    "age_group": self._age_group,
                    "sources": [],
                },
            )

        # Filter out very low-score hits (noise) — honest disclosure if nothing
        # truly relevant was retrieved
        RELEVANCE_THRESHOLD = 0.15
        relevant_hits = [h for h in hits if h.score >= RELEVANCE_THRESHOLD]
        if not relevant_hits:
            return AgentOutput(
                text=UNSURE_RESPONSE_TEMPLATE,
                emotion="neutral",
                intent="encyclopedia",
                metadata={
                    "retrieval_status": "low_relevance",
                    "question": actual_question,
                    "age_group": self._age_group,
                    "sources": [],
                },
            )
        hits = relevant_hits

        # 3. Compose the answer via LLM
        context_text = self._format_hits(hits)
        messages = [
            {"role": "system", "content": self.get_system_prompt_with_persona()},
            {"role": "user", "content": f"问题：{actual_question}\n\n参考资料：\n{context_text}"},
        ]

        try:
            response = await self.llm.chat(  # type: ignore[union-attr]
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
                },
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
        )
        return await self.invoke(child_input)
