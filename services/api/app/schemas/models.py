from typing import Literal

from pydantic import BaseModel, Field

from app.services.model_gateway import ModelHealth, ModelInfo


class ModelProfileOut(BaseModel):
    """The effective capabilities and prompt profile of a model (ADR C-09)."""

    model_id: str
    provider: str
    profile: Literal["lite", "standard", "advanced"]
    profile_source: str  # admin | default | auto
    context_tokens: int | None
    tool_calling: bool
    vision: bool
    reasoning_toggle: bool
    parallel_tools: bool
    structured_output: bool
    max_tool_iterations: int
    max_output_tokens: int | None
    reasoning_budget_tokens: int | None
    compact_tools: bool
    memory_items: int
    recall_chunks: int
    skill_index_items: int
    sources: dict[str, str]


class ModelListResponse(BaseModel):
    default_model_id: str
    models: list[ModelInfo]
    health: ModelHealth
    profiles: dict[str, ModelProfileOut] = Field(default_factory=dict)


class ModelRegistryEntry(BaseModel):
    model_id: str
    provider: str
    available: bool
    detected: dict
    profile_setting: Literal["lite", "standard", "advanced"] | None
    overrides: dict
    limits: dict
    notes: str | None
    effective: ModelProfileOut


class CapabilityOverrides(BaseModel):
    """Set by an administrator; null fields fall back to what was detected."""

    tool_calling: bool | None = None
    vision: bool | None = None
    parallel_tools: bool | None = None
    structured_output: bool | None = None
    reasoning_toggle: bool | None = None
    context_tokens: int | None = Field(default=None, ge=512, le=10_000_000)


class ModelLimits(BaseModel):
    max_tool_iterations: int | None = Field(default=None, ge=1, le=100)
    max_output_tokens: int | None = Field(default=None, ge=16, le=1_000_000)
    # Thinking tokens before the model is asked to conclude (llama.cpp). See
    # MODEL_REASONING_BUDGET_TOKENS for the deployment default.
    reasoning_budget_tokens: int | None = Field(default=None, ge=256, le=1_000_000)


class ModelRegistryUpdate(BaseModel):
    model_id: str = Field(min_length=1, max_length=255)
    profile: Literal["lite", "standard", "advanced"] | None = None  # null: automatic
    overrides: CapabilityOverrides = Field(default_factory=CapabilityOverrides)
    limits: ModelLimits = Field(default_factory=ModelLimits)
    notes: str | None = Field(default=None, max_length=2000)
