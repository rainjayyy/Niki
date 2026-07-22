"""Routing layer — intent classification, multi-agent routing, persona injection.

Public surface:
    IntentClassifier   — doubao-powered intent classifier (top-level + tutoring subtype)
    AgentRouter        — routes single/composite intents to the right agents
    FusionOrchestrator — merges outputs from multiple agents (composite intent)
    PersonaInjector    — fills {persona} placeholders + TTS parameter overrides
"""

from niki.routing.classifier import (
    CompositeIntent,
    Intent,
    IntentCategory,
    IntentClassifier,
    IntentResult,
    TutoringSubtype,
)
from niki.routing.fusion import (
    FusedOutput,
    FusionOrchestrator,
    FusionStrategy,
)
from niki.routing.persona import (
    Persona,
    PersonaInjector,
    PersonaPrompt,
    TTSParameters,
)
from niki.routing.router import AgentName, AgentRouter, RoutingDecision

__all__ = [
    "Intent",
    "IntentCategory",
    "IntentResult",
    "CompositeIntent",
    "TutoringSubtype",
    "IntentClassifier",
    "Persona",
    "PersonaInjector",
    "PersonaPrompt",
    "TTSParameters",
    "AgentName",
    "AgentRouter",
    "RoutingDecision",
    "FusedOutput",
    "FusionOrchestrator",
    "FusionStrategy",
]
