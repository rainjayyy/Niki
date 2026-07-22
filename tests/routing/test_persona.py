"""Tests for the persona injection layer."""

import pytest

from niki.routing.persona import Persona, PersonaInjector, TTSParameters


class TestPersonaDefaults:
    """Default persona = gentle."""

    def test_default_is_gentle(self) -> None:
        injector = PersonaInjector()
        assert injector.default == Persona.GENTLE

    def test_set_default(self) -> None:
        injector = PersonaInjector()
        injector.set_default(Persona.LIVELY)
        assert injector.default == Persona.LIVELY

    def test_set_unknown_raises(self) -> None:
        injector = PersonaInjector()
        with pytest.raises(ValueError):
            injector.set_default("unknown")  # type: ignore[arg-type]


class TestPersonaFragments:
    """Persona fragments contain the required PRD markers."""

    def test_lively_has_warm_markers(self) -> None:
        injector = PersonaInjector()
        prompt = injector.get_prompt(Persona.LIVELY)
        # PRD: 多感叹号和拟声词，说「哇塞」「太酷了」
        assert "哇塞" in prompt.fragment or "太酷了" in prompt.fragment

    def test_gentle_has_warmth_markers(self) -> None:
        injector = PersonaInjector()
        prompt = injector.get_prompt(Persona.GENTLE)
        # PRD: 多用拥抱和鼓励
        assert any(marker in prompt.fragment for marker in ["拥抱", "鼓励", "云朵", "柔软"])

    def test_rigorous_has_scholar_markers(self) -> None:
        injector = PersonaInjector()
        prompt = injector.get_prompt(Persona.RIGOROUS)
        # PRD: 像图书管理员般可靠
        assert "图书" in prompt.fragment or "可靠" in prompt.fragment


class TestPersonaTTSParameters:
    """TTS parameters per PRD table."""

    def test_lively_tts(self) -> None:
        injector = PersonaInjector()
        params = injector.get_tts_params(Persona.LIVELY)
        assert params.speech_rate == 1.1
        assert params.pitch > 0  # higher pitch

    def test_gentle_tts(self) -> None:
        injector = PersonaInjector()
        params = injector.get_tts_params(Persona.GENTLE)
        assert params.speech_rate == 0.9
        assert params.pitch == 0.0  # medium pitch

    def test_rigorous_tts(self) -> None:
        injector = PersonaInjector()
        params = injector.get_tts_params(Persona.RIGOROUS)
        assert params.speech_rate == 1.0
        assert params.pitch == 0.0

    def test_all_personas_have_valid_tts(self) -> None:
        injector = PersonaInjector()
        for persona in injector.available_personas():
            params = injector.get_tts_params(persona)
            assert isinstance(params, TTSParameters)
            assert 0.5 <= params.speech_rate <= 2.0
            assert -1.0 <= params.pitch <= 1.0


class TestPersonaInjection:
    """The injection logic itself."""

    def test_inject_prepends_persona_fragment(self) -> None:
        injector = PersonaInjector()
        base = "You are a tutor. Never give direct answers."
        injected = injector.inject(base, Persona.GENTLE)
        # Persona fragment should be at the start
        assert injected.startswith(injector.get_prompt(Persona.GENTLE).fragment)

    def test_inject_keeps_base_prompt(self) -> None:
        injector = PersonaInjector()
        base = "You are a tutor. Never give direct answers."
        injected = injector.inject(base, Persona.LIVELY)
        assert base in injected

    def test_inject_with_default_persona(self) -> None:
        injector = PersonaInjector()
        injector.set_default(Persona.LIVELY)
        injected = injector.inject("base prompt")
        assert injector.get_prompt(Persona.LIVELY).fragment in injected

    def test_injection_changes_when_persona_changes(self) -> None:
        injector = PersonaInjector()
        base = "base prompt"
        lively_injected = injector.inject(base, Persona.LIVELY)
        gentle_injected = injector.inject(base, Persona.GENTLE)
        assert lively_injected != gentle_injected


class TestAvailablePersonas:
    """List of available personas."""

    def test_three_personas_supported(self) -> None:
        injector = PersonaInjector()
        personas = injector.available_personas()
        assert len(personas) == 3
        assert Persona.LIVELY in personas
        assert Persona.GENTLE in personas
        assert Persona.RIGOROUS in personas
