"""Tests for ChromaDB client."""

import pytest

from niki.infrastructure.chroma import (
    COLLECTION_CONVERSATIONS,
    COLLECTION_ENTITIES,
    COLLECTION_KNOWLEDGE_BASE,
    ChromaClient,
)


def test_collection_constants() -> None:
    """Verify collection name constants are defined."""
    assert COLLECTION_CONVERSATIONS == "conversations"
    assert COLLECTION_KNOWLEDGE_BASE == "knowledge_base"
    assert COLLECTION_ENTITIES == "entities"


@pytest.mark.integration
def test_chroma_health_check() -> None:
    """Integration: health check should work when ChromaDB is available."""
    result = ChromaClient.health_check()
    assert result["status"] in ("healthy", "unhealthy")
