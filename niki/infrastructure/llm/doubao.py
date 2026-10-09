"""doubao LLM client — lightweight model for classification and preprocessing.

doubao is used for low-latency, non-child-facing tasks:
- Intent classification & routing
- Emotion detection & difficulty assessment
- Fact checking
- Output safety gateway
- Daytime memory hook (lightweight extraction)
"""

from typing import Any

import structlog

from niki.infrastructure.llm.base import BaseLLMClient

logger = structlog.get_logger(__name__)

# Valid task types for doubao (used in request metadata)
TaskType = str
TASK_CLASSIFICATION: TaskType = "classification"
TASK_CHECK: TaskType = "check"
TASK_PREPROCESS: TaskType = "preprocess"


class DoubaoClient(BaseLLMClient):
    """Client for doubao lightweight model via OpenAI-compatible API.

    Endpoint: {base_url}/chat/completions
    """

    @property
    def provider_name(self) -> str:
        return "doubao"

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,  # Lower default for classification tasks
        max_tokens: int = 512,  # Shorter responses for lightweight tasks
        task_type: TaskType = TASK_CLASSIFICATION,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request to doubao.

        Args:
            messages: Chat message history.
            temperature: 0.0–2.0, default lower for deterministic classification.
            max_tokens: Response token limit, default shorter for light tasks.
            task_type: Task category for observability (classification/check/preprocess).

        Returns:
            Parsed response dict with 'content', 'model', 'usage' keys.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Pass task_type as metadata — the doubao gateway may use it for routing
            "metadata": {"task_type": task_type},
            **kwargs,
        }

        logger.debug(
            "doubao.chat.request",
            model=self.model,
            task_type=task_type,
            message_count=len(messages),
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
        # Pass through tool_calls when present (OpenAI standard).
        # `tools=[...]` and `tool_choice=...` are already forwarded via **kwargs.
        if choice["message"].get("tool_calls"):
            result["tool_calls"] = choice["message"]["tool_calls"]

        logger.debug(
            "doubao.chat.response",
            model=result["model"],
            task_type=task_type,
            finish_reason=result["finish_reason"],
        )

        return result
