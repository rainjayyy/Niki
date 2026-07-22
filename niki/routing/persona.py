"""Persona injection layer — PRD #37, #38, #20, #21.

Three personas (per PRD):
  lively    — enthusiastic friend ("哇塞!太酷了!"), 1.1x speech rate, high pitch
  gentle    — patient preschool teacher, 0.9x speech rate, medium pitch
  rigorous  — reliable scholar, 1.0x speech rate, medium pitch

Design:
- PersonaInjector takes a base system prompt and returns:
  - the persona-augmented prompt (for LLM)
  - TTS parameter overrides (for voice synthesis)
- Persona is independent of agent logic — injecting doesn't change
  safety behaviour or guided-discovery rules (per PRD #38).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog

logger = structlog.get_logger(__name__)


class Persona(StrEnum):
    """Supported Niki personas."""

    LIVELY = "lively"
    GENTLE = "gentle"
    RIGOROUS = "rigorous"


@dataclass(frozen=True)
class TTSParameters:
    """TTS parameter overrides tied to a persona."""

    speech_rate: float         # multiplier: 0.5 - 2.0
    pitch: float               # -1.0 to 1.0 (negative=low, positive=high)
    volume: float = 1.0        # 0.0 - 1.0
    emotion: str = "neutral"   # TTS-specific emotion tag


@dataclass(frozen=True)
class PersonaPrompt:
    """The persona fragment that gets injected into an agent's system prompt."""

    persona: Persona
    fragment: str
    tts: TTSParameters


# Persona definitions (PRD #37, #38 — TTS parameters table)
_PERSONA_DEFINITIONS: dict[Persona, PersonaPrompt] = {
    Persona.LIVELY: PersonaPrompt(
        persona=Persona.LIVELY,
        fragment=(
            "你是一个热情阳光的AI伙伴，名叫Niki。"
            "多用感叹号和拟声词，说「哇塞」「太酷了」——像阳光般热情。"
            "用活泼的语气和小朋友交流。"
        ),
        tts=TTSParameters(
            speech_rate=1.1,
            pitch=0.4,
            volume=1.0,
            emotion="happy",
        ),
    ),
    Persona.GENTLE: PersonaPrompt(
        persona=Persona.GENTLE,
        fragment=(
            "你是一个温柔耐心的AI伙伴，名叫Niki。"
            "说话轻柔缓慢，多用拥抱和鼓励的话语——像云朵般柔软。"
            "像耐心的幼儿园老师一样呵护小朋友。"
        ),
        tts=TTSParameters(
            speech_rate=0.9,
            pitch=0.0,
            volume=0.95,
            emotion="warm",
        ),
    ),
    Persona.RIGOROUS: PersonaPrompt(
        persona=Persona.RIGOROUS,
        fragment=(
            "你是一个可靠严谨的AI伙伴，名叫Niki。"
            "说话准确认真，不夸夸其谈也不冷漠——像图书管理员般可靠。"
            "用清晰有条理的方式帮助小朋友理解世界。"
        ),
        tts=TTSParameters(
            speech_rate=1.0,
            pitch=0.0,
            volume=1.0,
            emotion="neutral",
        ),
    ),
}


class PersonaInjector:
    """Builds persona-augmented system prompts + TTS parameters."""

    def __init__(self, default: Persona = Persona.GENTLE) -> None:
        self._default = default

    @property
    def default(self) -> Persona:
        """The currently selected default persona."""
        return self._default

    def set_default(self, persona: Persona) -> None:
        """Change the default persona."""
        if persona not in _PERSONA_DEFINITIONS:
            raise ValueError(f"Unknown persona: {persona}")
        self._default = persona

    def get_prompt(self, persona: Persona | None = None) -> PersonaPrompt:
        """Return the persona fragment + TTS parameters.

        Args:
            persona: Override the default for this call.
        """
        target = persona or self._default
        return _PERSONA_DEFINITIONS[target]

    def inject(
        self,
        base_system_prompt: str,
        persona: Persona | None = None,
    ) -> str:
        """Prepend the persona fragment to an agent's base system prompt.

        The fragment establishes the "voice" of Niki; the base prompt
        carries the agent-specific logic (tutoring guidance, encyclopedia
        rules, emotional response patterns, etc.).
        """
        prompt = self.get_prompt(persona)
        injected = f"{prompt.fragment}\n\n{base_system_prompt}"
        logger.debug(
            "persona_injector.inject",
            persona=prompt.persona.value,
            base_length=len(base_system_prompt),
            injected_length=len(injected),
        )
        return injected

    def get_tts_params(self, persona: Persona | None = None) -> TTSParameters:
        """Return the TTS parameter overrides for the given persona."""
        return self.get_prompt(persona).tts

    def available_personas(self) -> list[Persona]:
        """List all supported personas."""
        return list(_PERSONA_DEFINITIONS.keys())
