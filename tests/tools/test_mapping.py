"""Unit tests for niki.tools.mapping (BE-AGENT-001)."""

from __future__ import annotations

import pytest

from niki.tools.mapping import (
    agent_type_to_matrix_key,
    matrix_key_to_agent_type,
)


class TestAgentTypeToMatrixKey:
    def test_known_short_forms(self) -> None:
        assert agent_type_to_matrix_key("encyclopedia") == "encyclopedia_agent"
        assert agent_type_to_matrix_key("tutoring") == "tutoring_agent"
        # The irregular form (emotion → emotional_agent) is the one
        # that historically caused silent denials in the matrix.
        assert agent_type_to_matrix_key("emotion") == "emotional_agent"

    def test_unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown agent_type"):
            agent_type_to_matrix_key("not_a_real_agent")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            agent_type_to_matrix_key("")


class TestMatrixKeyToAgentType:
    def test_known_keys(self) -> None:
        assert matrix_key_to_agent_type("encyclopedia_agent") == "encyclopedia"
        assert matrix_key_to_agent_type("tutoring_agent") == "tutoring"
        assert matrix_key_to_agent_type("emotional_agent") == "emotion"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown matrix key"):
            matrix_key_to_agent_type("ghost_agent")

    def test_round_trip(self) -> None:
        for short in ("encyclopedia", "tutoring", "emotion"):
            assert matrix_key_to_agent_type(agent_type_to_matrix_key(short)) == short
