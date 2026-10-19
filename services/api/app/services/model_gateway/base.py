"""Provider-neutral model gateway interface.

The rest of the application depends only on these types, never on a specific
inference server's wire format.
"""

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ModelGatewayError(Exception):
    """Raised for provider failures. `code` is stable and safe to show to clients."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ModelCapabilities(BaseModel):
    streaming: bool = True
    tool_calling: bool = False
    vision: bool = False
    embeddings: bool = False
    # The chat template accepts `enable_thinking` (e.g. Qwen3, DeepSeek-R1 distills), so
    # reasoning can be switched on and off per request.
    reasoning_toggle: bool = False
    # How the capability was established: "configured" (env flag), "reported" (server said so),
    # or "verified" (a live probe succeeded). Only "verified" should be trusted for tools.
    source: Literal["configured", "reported", "verified"] = "configured"


class ModelInfo(BaseModel):
    id: str
    provider: str
    context_tokens: int | None = None
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class ModelHealth(BaseModel):
    ok: bool
    provider: str
    latency_ms: float | None = None
    detail: str | None = None


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    # None: the model's default. True/False: ask the template to think or not.
    reasoning: bool | None = None


class ToolCallDelta(BaseModel):
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


class StreamChunk(BaseModel):
    text: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCallDelta] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


class ModelGateway(Protocol):
    provider: str

    async def list_models(self, *, max_age_seconds: float = 0) -> list[ModelInfo]: ...

    async def find_model(
        self, model_id: str, *, max_age_seconds: float = 60
    ) -> ModelInfo | None: ...

    async def get_capabilities(self, model_id: str) -> ModelCapabilities: ...

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]: ...

    async def count_tokens(self, text: str) -> int | None: ...

    async def create_embeddings(
        self, texts: list[str], model: str | None = None
    ) -> list[list[float]]: ...

    async def health_check(self) -> ModelHealth: ...

    async def aclose(self) -> None: ...
