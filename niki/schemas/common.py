"""Common Pydantic schemas used across the application."""

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# --- API Response Wrappers ---


class HealthStatus(BaseModel):
    """Health status of a single infrastructure component."""

    component: str = Field(..., description="Name of the infrastructure component")
    status: str = Field(..., description="'healthy' | 'degraded' | 'unhealthy'")
    latency_ms: float | None = Field(None, description="Response latency in milliseconds")
    error: str | None = Field(None, description="Error message if unhealthy")


class HealthResponse(BaseModel):
    """Response model for the /health endpoint."""

    status: str = Field(..., description="Overall health: 'healthy' | 'degraded' | 'unhealthy'")
    version: str = Field(..., description="Application version")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    components: list[HealthStatus] = Field(default_factory=list)


class APIResponse(BaseModel, Generic[T]):
    """Standard API response wrapper — BE-API-001 §6.3.

    Success shape:
        {"success": true, "data": {...}, "request_id": "uuid"}

    Error shape (BE-API-001 §6.3):
        {"success": false,
         "error": {"code": "INVALID_PHONE_FORMAT",
                   "message": "phone format invalid",
                   "detail": {...}              # optional
                  },
         "request_id": "uuid"}

    Back-compat fields (kept so old frontends keep working):
        error_code   - flat string, mirrors error.code
        error_detail - flat dict, mirrors error.detail

    BE-API-001 升级版：新前端用 `error.code`；老前端继续读 `error` (字符串)
    与 `error_code` 任一可用。
    """

    success: bool = True
    data: T | None = None
    error: str | dict[str, Any] | None = None
    error_code: str | None = None
    error_detail: dict[str, Any] | None = None
    request_id: str | None = None

    @classmethod
    def from_error(
        cls,
        *,
        code: str,
        message: str,
        request_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> "APIResponse":
        """Helper to build a structured error response."""
        return cls(
            success=False,
            error={"code": code, "message": message, "detail": detail or {}},
            error_code=code,
            error_detail=detail,
            request_id=request_id,
        )


class PaginatedResponse(BaseModel, Generic[T]):
    """Paginated API response wrapper."""

    success: bool = True
    data: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    has_more: bool = False


class ErrorDetail(BaseModel):
    """Structured error detail for API error responses."""

    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")
    detail: dict[str, Any] | None = Field(None, description="Additional error context")


# --- Agent-related schemas ---


class ChildInput(BaseModel):
    """Input from the child user."""

    text: str | None = Field(None, description="Transcribed text from child's speech")
    image_url: str | None = Field(None, description="URL of an image the child shared")
    audio_duration_ms: int | None = Field(None, description="Duration of the audio input")
    session_id: str = Field(..., description="Current session identifier")


class AgentOutput(BaseModel):
    """Output from any Niki agent."""

    text: str = Field(..., description="The text response for the child")
    emotion: str | None = Field(None, description="Detected emotion in the child's input")
    intent: str | None = Field(None, description="Classified intent of the child's input")
    should_end_session: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- Persona ---


class PersonaType(str):
    """Supported Niki personas."""

    LIVELY = "lively"  # 活泼 — enthusiastic friend
    GENTLE = "gentle"  # 温柔 — patient preschool teacher
    RIGOROUS = "rigorous"  # 严谨 — reliable scholar


# --- Intent ---


class IntentCategory(str):
    """Top-level intent categories from the classifier."""

    ENCYCLOPEDIA = "encyclopedia"
    TUTORING = "tutoring"
    EMOTIONAL = "emotional"
    COMPOSITE = "composite"


class TutoringSubtype(str):
    """Sub-classification of tutoring intents."""

    PURE_CALCULATION = "pure_calculation"
    WORD_PROBLEM = "word_problem"
    VISUAL_PROBLEM = "visual_problem"
    OPEN_ENDED = "open_ended"
