"""Abstract base class for LLM clients."""

from abc import ABC, abstractmethod
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
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Lazy initialization of the httpx async client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                headers=self._default_headers(),
            )
        return self._client

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
            **kwargs: Provider-specific extra parameters.

        Returns:
            The parsed response from the LLM provider.
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
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _handle_http_error(self, status_code: int, model: str) -> None:
        """Map HTTP status codes to Niki exceptions."""
        if status_code == 401:
            raise LLMAuthenticationError(model)
        elif status_code == 429:
            raise LLMRateLimitError(model)
        elif status_code >= 500:
            raise LLMTimeoutError(model, timeout=self.timeout)
