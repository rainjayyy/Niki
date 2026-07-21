"""Base agent class for all Niki agents.

All agents (Encyclopedia, Tutoring, Emotion) inherit from BaseAgent,
which provides LLM integration, persona injection, and the LangChain
Runnable interface.
"""

from abc import ABC, abstractmethod
from typing import Any

import structlog

from niki.infrastructure.llm.base import BaseLLMClient
from niki.schemas.common import AgentOutput, ChildInput

logger = structlog.get_logger(__name__)

# Persona System Prompt fragments (from PRD)
PERSONA_PROMPTS: dict[str, str] = {
    "lively": (
        "你是一个热情阳光的AI伙伴，名叫Niki。"
        "多用感叹号和拟声词，说「哇塞」「太酷了」——像阳光般热情。"
        "用活泼的语气和小朋友交流。"
    ),
    "gentle": (
        "你是一个温柔耐心的AI伙伴，名叫Niki。"
        "说话轻柔缓慢，多用拥抱和鼓励的话语——像云朵般柔软。"
        "像耐心的幼儿园老师一样呵护小朋友。"
    ),
    "rigorous": (
        "你是一个可靠严谨的AI伙伴，名叫Niki。"
        "说话准确认真，不夸夸其谈也不冷漠——像图书管理员般可靠。"
        "用清晰有条理的方式帮助小朋友理解世界。"
    ),
}

# Age-adaptive content mode constraints (from PRD)
AGE_CONSTRAINTS: dict[str, dict[str, int]] = {
    "3-5": {"max_sentence_length": 10, "max_response_sentences": 5},
    "6-8": {"max_sentence_length": 15, "max_response_sentences": 8},
    "9-12": {"max_sentence_length": 20, "max_response_sentences": 12},
}


class BaseAgent(ABC):
    """Abstract base for all Niki agents.

    Each agent handles one intent domain:
    - EncyclopediaAgent → "encyclopedia"
    - TutoringAgent → "tutoring"
    - EmotionAgent → "emotional"

    Subclasses implement `_build_system_prompt()` and `_process()`.
    """

    agent_type: str = "base"

    def __init__(
        self,
        llm: BaseLLMClient,
        persona: str = "gentle",
        child_age: int = 7,
    ) -> None:
        """Initialize the agent.

        Args:
            llm: The LLM client to use for inference.
            persona: One of 'lively', 'gentle', 'rigorous'.
            child_age: Child's age for content adaptation.
        """
        self.llm = llm
        self.persona = persona
        self.child_age = child_age
        self._age_group = self._resolve_age_group(child_age)

    @property
    def persona_prompt(self) -> str:
        """Get the persona system prompt fragment."""
        return PERSONA_PROMPTS.get(self.persona, PERSONA_PROMPTS["gentle"])

    @property
    def age_constraints(self) -> dict[str, int]:
        """Get age-adaptive content constraints."""
        return AGE_CONSTRAINTS.get(self._age_group, AGE_CONSTRAINTS["6-8"])

    @staticmethod
    def _resolve_age_group(age: int) -> str:
        """Map child's age to an age group."""
        if age <= 5:
            return "3-5"
        elif age <= 8:
            return "6-8"
        else:
            return "9-12"

    @abstractmethod
    def _build_system_prompt(self) -> str:
        """Build the system prompt for this agent.

        Must include security constraints (from PRD):
        - Never ask for private information
        - Never output direct answers in tutoring mode
        - Age-appropriate language
        """
        ...

    @abstractmethod
    async def _process(
        self,
        child_input: ChildInput,
        context: dict[str, Any] | None = None,
    ) -> AgentOutput:
        """Process the child's input and produce an agent response.

        Args:
            child_input: The child's transcribed input and metadata.
            context: Optional conversation context (memory, emotion state, etc.).

        Returns:
            AgentOutput with the response text and metadata.
        """
        ...

    async def invoke(
        self,
        child_input: ChildInput,
        context: dict[str, Any] | None = None,
    ) -> AgentOutput:
        """Public entry point: invoke the agent.

        Calls `_process()` with logging and error handling.
        """
        logger.info(
            "agent.invoke",
            agent_type=self.agent_type,
            persona=self.persona,
            age_group=self._age_group,
            session_id=child_input.session_id,
        )

        try:
            output = await self._process(child_input, context)
            logger.info(
                "agent.invoke.complete",
                agent_type=self.agent_type,
                emotion=output.emotion,
                intent=output.intent,
            )
            return output
        except Exception as exc:
            logger.error(
                "agent.invoke.failed",
                agent_type=self.agent_type,
                error=str(exc),
                exc_info=True,
            )
            # Return a safe fallback response
            return AgentOutput(
                text="嗯…Niki需要想一想，稍等一下哦~",
                emotion="neutral",
                intent=self.agent_type,
            )

    def get_system_prompt_with_persona(self) -> str:
        """Combine the agent's system prompt with the persona fragment."""
        base_prompt = self._build_system_prompt()
        return f"{self.persona_prompt}\n\n{base_prompt}"

    def get_age_guardrails(self) -> str:
        """Get age-adaptive guardrails as a prompt fragment."""
        constraints = self.age_constraints
        return (
            f"每句话不超过{constraints['max_sentence_length']}个字。"
            f"每次回答不超过{constraints['max_response_sentences']}句话。"
        )
