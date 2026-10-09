"""Abstract base class for LLM clients."""

from abc import ABC, abstractmethod
import asyncio
from typing import Any

import httpx
import structlog
from tenacity import (
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from niki.utils.exceptions import LLMAuthenticationError, LLMRateLimitError, LLMTimeoutError

logger = structlog.get_logger(__name__)

# Standard retry policy for LLM API calls
DEFAULT_RETRY_POLICY = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=1, min=1, max=10),
    "retry": retry_if_exception_type((LLMTimeoutError, LLMRateLimitError)),
}


class BaseLLMClient(ABC):
    """Abstract base for all LLM API clients.

    Subclasses implement `_build_request` and `_parse_response` to handle
    provider-specific API differences, while the base class provides
    retry logic, error handling, health checking, and streaming support.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        # httpx AsyncClient instances are bound to the event loop they
        # were first used on. TestClient / multi-loop environments can
        # close one loop and reuse the same client from another, which
        # surfaces as "Event loop is closed". Cache one client per loop
        # so each loop gets a fresh connection pool.
        self._clients: dict[int, tuple[asyncio.AbstractEventLoop | None, httpx.AsyncClient]] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        """Return an httpx client bound to the current event loop."""
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        key = id(loop) if loop is not None else 0
        entry = self._clients.get(key)
        if entry is not None and entry[0] is loop:
            return entry[1]

        client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
            headers=self._default_headers(),
        )
        self._clients[key] = (loop, client)
        return client

    def _default_headers(self) -> dict[str, str]:
        """Build default HTTP headers for API requests."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name for logging and errors."""
        ...

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.
            temperature: Sampling temperature (0.0–2.0).
            max_tokens: Maximum tokens in the response.
            **kwargs: Provider-specific extra parameters. OpenAI-style
                tool calling is supported via ``tools=[...]`` and
                ``tool_choice=...`` — forwarded verbatim to the
                upstream API.

        Returns:
            The parsed response from the LLM provider. When the model
            issues a tool call the response dict also carries a
            ``tool_calls`` key (list of OpenAI-standard call objects
            with ``id``, ``type``, ``function.name``,
            ``function.arguments``). When no tool call is made the
            key is absent.
        """
        ...

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Any:
        """Send a streaming chat completion request.

        Override in subclasses that support streaming.
        """
        raise NotImplementedError(f"Streaming not supported for {self.provider_name}")

    async def health_check(self) -> bool:
        """Check if the LLM API is reachable and responding.

        Returns:
            True if the API responds successfully, False otherwise.
        """
        try:
            response = await self.client.get("/models")
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close every underlying HTTP client."""
        for _loop, client in list(self._clients.values()):
            await client.aclose()
        self._clients.clear()

    def _handle_http_error(self, status_code: int, model: str) -> None:
        """Map HTTP status codes to Niki exceptions."""
        if status_code == 401:
            raise LLMAuthenticationError(model)
        elif status_code == 429:
            raise LLMRateLimitError(model)
        elif status_code >= 500:
            raise LLMTimeoutError(model, timeout=self.timeout)
