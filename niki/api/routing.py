"""Routing API endpoints — intent classification, routing decisions, persona management.

Demonstration endpoints for the #2 vertical slice:
  POST /classify      — classify input → IntentResult
  POST /route         — classify + route → RoutingDecision
  POST /persona/inject — inject persona fragment into a base system prompt
  GET  /personas      — list available personas + their TTS parameters

In production these would be invoked from the chat pipeline; here they
are exposed for direct testing and integration.
"""

from __future__ import annotations

from fastapi import APIRouter

from niki.routing import (
    AgentName,
    AgentRouter,
    FusionOrchestrator,
    IntentClassifier,
    Persona,
    PersonaInjector,
)

router = APIRouter(
    prefix="/routing",
    tags=[
        {
            "name": "routing",
            "description": (
                "Routing API endpoints — intent classification, "
                "routing decisions, persona management."
            ),
        },
    ],
)

# Module-level singletons (cheap to construct, safe to reuse)
_classifier = IntentClassifier()
_router = AgentRouter()
_fusion = FusionOrchestrator()
_persona = PersonaInjector()


def _intent_result_to_dict(result) -> dict:
    """Serialise an IntentResult to JSON-safe dict."""
    return {
        "raw_text": result.raw_text,
        "threshold": result.threshold,
        "is_composite": result.is_composite,
        "primary": (
            {
                "category": result.primary.category.value,
                "confidence": result.primary.confidence,
                "tutoring_subtype": (
                    result.primary.tutoring_subtype.value
                    if result.primary.tutoring_subtype
                    else None
                ),
            }
            if result.primary
            else None
        ),
        "intents": [
            {
                "category": i.category.value,
                "confidence": i.confidence,
                "tutoring_subtype": (i.tutoring_subtype.value if i.tutoring_subtype else None),
            }
            for i in result.intents
        ],
    }


def _routing_decision_to_dict(decision) -> dict:
    """Serialise a RoutingDecision."""
    return {
        "primary_agent": decision.primary_agent.value,
        "parallel_agents": [a.value for a in decision.parallel_agents],
        "is_composite": decision.is_composite,
        "all_agents": [a.value for a in decision.all_agents],
        "rationale": decision.rationale,
        "intent_result": (
            _intent_result_to_dict(decision.intent_result) if decision.intent_result else None
        ),
    }


@router.post(
    "/classify",
    summary="Classify input text into intent categories",
    description="""
            Classify input text into intent categories.
            
            Request body: {"text": "..."}
            """,
)
async def classify(payload: dict) -> dict:
    """Classify input text into intent categories.

    Request body: {"text": "..."}
    """
    text = payload.get("text", "")
    result = await _classifier.classify(text)
    return _intent_result_to_dict(result)


@router.post(
    "/route",
    summary="Classify and route to the appropriate agent(s)",
    description="""
            Classify and route to the appropriate agent(s).
            
            Request body: {"text": "..."}
            """,
)
async def route(payload: dict) -> dict:
    """Classify and route to the appropriate agent(s).

    Request body: {"text": "..."}
    """
    text = payload.get("text", "")
    result = await _classifier.classify(text)
    decision = _router.route(result)
    return _routing_decision_to_dict(decision)


@router.get(
    "/personas",
    summary="List all available personas with their TTS parameters",
    description="List all available personas with their TTS parameters.",
)
async def list_personas() -> dict:
    """List all available personas with their TTS parameters."""
    return {
        "personas": [
            {
                "name": p.value,
                "fragment": _persona.get_prompt(p).fragment,
                "tts": {
                    "speech_rate": _persona.get_tts_params(p).speech_rate,
                    "pitch": _persona.get_tts_params(p).pitch,
                    "volume": _persona.get_tts_params(p).volume,
                    "emotion": _persona.get_tts_params(p).emotion,
                },
            }
            for p in _persona.available_personas()
        ],
        "default": _persona.default.value,
    }


@router.post(
    "/persona/inject",
    summary="Inject the persona fragment into a base system prompt",
    description="""
            Inject the persona fragment into a base system prompt.
            
            Request body: {"base_prompt": "...", "persona": "lively|gentle|rigorous"}
            """,
)
async def inject_persona(payload: dict) -> dict:
    """Inject the persona fragment into a base system prompt.

    Request body: {"base_prompt": "...", "persona": "lively|gentle|rigorous"}
    """
    base = payload.get("base_prompt", "")
    persona_value = payload.get("persona")
    persona: Persona | None = None
    if persona_value is not None:
        try:
            persona = Persona(persona_value)
        except ValueError:
            return {"error": f"unknown persona: {persona_value}"}

    injected = _persona.inject(base, persona)
    tts_params = _persona.get_tts_params(persona)

    return {
        "persona": (persona or _persona.default).value,
        "injected_prompt": injected,
        "tts_parameters": {
            "speech_rate": tts_params.speech_rate,
            "pitch": tts_params.pitch,
            "volume": tts_params.volume,
            "emotion": tts_params.emotion,
        },
        "fragment_length": len(_persona.get_prompt(persona).fragment),
    }


@router.post(
    "/fuse",
    summary="Demonstrate fusion of mocked agent outputs",
    description="""
            Demonstrate fusion of mocked agent outputs.
            
            Request body:
                {
                  "decision": {"primary_agent": "...", "parallel_agents": [...]},
                  "outputs": {"agent_name": {"text": "...", "emotion": "...", ...}}
                }
            """,
)
async def fuse_agents(payload: dict) -> dict:
    """Demonstrate fusion of mocked agent outputs.

    Request body:
        {
          "decision": {"primary_agent": "...", "parallel_agents": [...]},
          "outputs": {"agent_name": {"text": "...", "emotion": "...", ...}}
        }
    """
    from niki.routing.classifier import IntentResult
    from niki.routing.router import RoutingDecision
    from niki.schemas.common import AgentOutput

    decision_payload = payload.get("decision", {})
    outputs_payload = payload.get("outputs", {})

    primary_agent = AgentName(decision_payload.get("primary_agent", "emotional_agent"))
    parallel = [AgentName(a) for a in decision_payload.get("parallel_agents", [])]

    # Rebuild minimal RoutingDecision directly
    decision = RoutingDecision(
        primary_agent=primary_agent,
        parallel_agents=parallel,
        intent_result=IntentResult(intents=[]),
    )

    outputs: dict[AgentName, AgentOutput] = {}
    for name, out in outputs_payload.items():
        try:
            outputs[AgentName(name)] = AgentOutput(**out)
        except (ValueError, KeyError):
            continue

    fused = _fusion.fuse(decision, outputs)
    return {
        "text": fused.text,
        "strategy": fused.strategy.value,
        "contributing_agents": [a.value for a in fused.contributing_agents],
    }
