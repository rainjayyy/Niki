"""Safety pipeline for Niki — input filtering and output gating.

This package implements the safety layer that sits between the child's
input and the LLM (input safety gateway), and between the LLM's output
and the TTS (output safety gateway).

Public surface:
    InputSafetyGateway  — Llama Guard + sensitive-word regex + topic blacklist downgrade
    OutputSafetyGateway — fear-word rewriting, privacy reverse-question interception,
                          direct-answer detection, age-inappropriate downgrading
    TopicBlacklist      — "downgrade" not "block" strategy
"""

from niki.safety.dictionaries import (
    DIRECT_ANSWER_PATTERNS,
    FEAR_WORDS,
    PRIVACY_QUESTION_PATTERNS,
    SENSITIVE_WORDS,
)
from niki.safety.input_gateway import InputSafetyGateway, InputVerdict
from niki.safety.output_gateway import OutputSafetyGateway, OutputVerdict
from niki.safety.topic_blacklist import TopicBlacklist, TopicMatch

__all__ = [
    "InputSafetyGateway",
    "InputVerdict",
    "OutputSafetyGateway",
    "OutputVerdict",
    "TopicBlacklist",
    "TopicMatch",
    "FEAR_WORDS",
    "SENSITIVE_WORDS",
    "PRIVACY_QUESTION_PATTERNS",
    "DIRECT_ANSWER_PATTERNS",
]
