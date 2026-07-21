"""DeepSeek-V4 LLM client — OpenAI-compatible API.

DeepSeek-V4 is used for deep reasoning tasks:
- Tutoring Agent guided discovery
- Encyclopedia Agent answer generation
- Emotional Agent empathetic responses
- Nightly pipeline entity extraction
- Fusion orchestrator output synthesis
"""

from typing import Any

import structlog

from niki.infrastructure.llm.base import BaseLLMClient

logger = structlog.get_logger(__name__)


class DeepSeekClient(BaseLLMClient):
    """Client for DeepSeek-V4 via OpenAI-compatible chat completions API.

    Endpoint: {base_url}/chat/completions
    """

    @property
    def provider_name(self) -> str:
        return "DeepSeek-V4"

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request to DeepSeek-V4.

        Args:
            messages: Chat message history.
            temperature: 0.0–2.0, higher = more creative.
            max_tokens: Response token limit.

        Returns:
            Parsed response dict with 'content', 'model', 'usage' keys.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        logger.debug(
            "deepseek.chat.request",
            model=self.model,
            message_count=len(messages),
            temperature=temperature,
        )

        response = await self.client.post("/chat/completions", json=payload)
        self._handle_http_error(response.status_code, self.provider_name)
        response.raise_for_status()

        data = response.json()
        choice = data["choices"][0]

        result: dict[str, Any] = {
            "content": choice["message"]["content"],
            "model": data.get("model", self.model),
            "usage": data.get("usage", {}),
            "finish_reason": choice.get("finish_reason", "stop"),
        }

        logger.debug(
            "deepseek.chat.response",
            model=result["model"],
            finish_reason=result["finish_reason"],
            usage=result["usage"],
        )

        return result

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Any:
        """Stream a chat completion response from DeepSeek-V4."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            **kwargs,
        }

        logger.debug("deepseek.chat.stream.start", model=self.model)

        response = await self.client.send(
            method="POST",
            url="/chat/completions",
            json=payload,
            stream=True,
        )
        self._handle_http_error(response.status_code, self.provider_name)
        response.raise_for_status()

        # Return the raw httpx streaming response for the caller to iterate
        return response
