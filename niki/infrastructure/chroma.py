"""ChromaDB vector store client.

Chroma is used for:
- Long-term memory: conversation embeddings with emotion values, topic tags, timestamps
- Knowledge base: L1 core + L2 extended children's encyclopedia
- Entity alignment: semantic similarity deduplication (cosine > 0.85)

Note: ChromaDB HTTP client is used in development; PGVector replaces it in production.
"""

import time
from typing import Any

import chromadb
import httpx
import structlog

from niki.config.settings import get_settings
from niki.utils.exceptions import VectorStoreError

logger = structlog.get_logger(__name__)

# Collection names
COLLECTION_CONVERSATIONS = "conversations"
COLLECTION_KNOWLEDGE_BASE = "knowledge_base"
COLLECTION_ENTITIES = "entities"


class ChromaClient:
    """Singleton ChromaDB client.

    Usage:
        chroma = await ChromaClient.get_client()
        collection = chroma.get_collection("conversations")
    """

    # NOTE: chromadb >= 1.0 exposes HttpClient as a factory function, not a class,
    # so we type the singleton as `object` since chromadb does not publish a stable type.
    _client: object | None = None

    @classmethod
    def get_client(cls) -> object:
        """Get or create the ChromaDB HTTP client.

        Returns:
            An initialized chromadb HttpClient.

        Raises:
            VectorStoreError: If connection fails.
        """
        if cls._client is not None:
            return cls._client

        settings = get_settings()
        logger.info(
            "chroma.connecting",
            host=settings.chroma_host,
            port=settings.chroma_port,
        )

        try:
            cls._client = chromadb.HttpClient(  # type: ignore[assignment]
                host=settings.chroma_host,
                port=settings.chroma_port,
            )
            # Verify connection by listing collections (lightweight ping)
            cls._client.list_collections()
            logger.info("chroma.connected")
        except Exception as exc:
            logger.error("chroma.connection_failed", error=str(exc))
            raise VectorStoreError(f"ChromaDB connection failed: {exc}") from exc

        return cls._client

    @classmethod
    def ensure_collections(cls) -> None:
        """Create the standard Chroma collections if they don't exist.

        Should be called during application startup.
        """
        client = cls.get_client()
        existing = {c.name for c in client.list_collections()}

        for name in [COLLECTION_CONVERSATIONS, COLLECTION_KNOWLEDGE_BASE, COLLECTION_ENTITIES]:
            if name not in existing:
                logger.info("chroma.creating_collection", collection=name)
                client.create_collection(name=name)
            else:
                logger.debug("chroma.collection_exists", collection=name)

    @classmethod
    def health_check(cls) -> dict[str, Any]:
        """Check ChromaDB connectivity via a lightweight HTTP heartbeat.

        Uses the /api/v2/heartbeat endpoint directly to avoid the heavier
        chromadb client telemetry calls. Returns a dict with status and latency.

        Returns:
            Dict with 'status' and 'latency_ms' keys.
        """
        settings = get_settings()
        url = f"http://{settings.chroma_host}:{settings.chroma_port}/api/v2/heartbeat"
        try:
            start = time.monotonic()
            with httpx.Client(timeout=5.0) as http:
                resp = http.get(url)
            latency = (time.monotonic() - start) * 1000
            if resp.status_code == 200:
                return {"status": "healthy", "latency_ms": round(latency, 2)}
            return {"status": "unhealthy", "latency_ms": None, "error": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"status": "unhealthy", "latency_ms": None, "error": str(exc)}

    @classmethod
    def close(cls) -> None:
        """Reset the client (ChromaDB HTTP client has no explicit close)."""
        cls._client = None
        logger.info("chroma.disconnected")
