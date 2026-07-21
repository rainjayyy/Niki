"""Custom exception hierarchy for Niki.

All application-level exceptions inherit from NikiException,
making it easy to catch and handle them uniformly.
"""


class NikiException(Exception):
    """Base exception for all Niki-specific errors."""

    def __init__(self, message: str = "An unexpected error occurred in Niki") -> None:
        self.message = message
        super().__init__(self.message)


# --- Configuration ---


class ConfigurationError(NikiException):
    """Raised when required configuration is missing or invalid."""

    def __init__(self, message: str = "Invalid or missing configuration") -> None:
        super().__init__(message)


# --- Infrastructure ---


class InfrastructureError(NikiException):
    """Base exception for infrastructure-related errors."""

    def __init__(self, message: str = "Infrastructure service error") -> None:
        super().__init__(message)


class DatabaseConnectionError(InfrastructureError):
    """Raised when a database connection cannot be established."""

    def __init__(self, service: str = "database", detail: str = "") -> None:
        msg = f"Failed to connect to {service}"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class CacheConnectionError(InfrastructureError):
    """Raised when a cache (Redis) connection cannot be established."""

    def __init__(self, detail: str = "") -> None:
        msg = "Failed to connect to Redis cache"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class VectorStoreError(InfrastructureError):
    """Raised when a vector store operation fails."""

    def __init__(self, detail: str = "") -> None:
        msg = "Vector store operation failed"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class GraphDatabaseError(InfrastructureError):
    """Raised when a Neo4j operation fails."""

    def __init__(self, detail: str = "") -> None:
        msg = "Graph database operation failed"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


# --- LLM ---


class LLMError(NikiException):
    """Base exception for LLM-related errors."""

    def __init__(self, model: str = "unknown", message: str = "LLM error") -> None:
        self.model = model
        super().__init__(f"[{model}] {message}")


class LLMTimeoutError(LLMError):
    """Raised when an LLM API call times out."""

    def __init__(self, model: str = "unknown", timeout: int = 0) -> None:
        super().__init__(model, f"Request timed out after {timeout}s")


class LLMRateLimitError(LLMError):
    """Raised when the LLM API rate limit is exceeded."""

    def __init__(self, model: str = "unknown") -> None:
        super().__init__(model, "Rate limit exceeded")


class LLMAuthenticationError(LLMError):
    """Raised when LLM API authentication fails."""

    def __init__(self, model: str = "unknown") -> None:
        super().__init__(model, "Authentication failed — check API key")


class LLMInvalidResponseError(LLMError):
    """Raised when the LLM response cannot be parsed or is malformed."""

    def __init__(self, model: str = "unknown", detail: str = "") -> None:
        msg = "Invalid or malformed response from model"
        if detail:
            msg += f": {detail}"
        super().__init__(model, msg)


# --- Validation ---


class ValidationError(NikiException):
    """Raised when input validation fails."""

    def __init__(self, message: str = "Validation failed") -> None:
        super().__init__(message)


class SafetyViolationError(ValidationError):
    """Raised when input or output violates safety constraints."""

    def __init__(self, message: str = "Content violated safety constraints") -> None:
        super().__init__(message)
