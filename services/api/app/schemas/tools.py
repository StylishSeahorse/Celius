import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models import ToolCallStatus

DecisionValue = Literal["allow", "ask", "deny"]
BuiltinProfile = Literal["read_only", "standard", "developer", "administrator"]


class ToolCallOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    step: int
    position: int
    tool_name: str
    risk: str
    arguments: dict[str, Any]
    # Approving requires quoting this: SHA-256 of the canonical JSON of `arguments`.
    args_hash: str
    status: ToolCallStatus
    decision_source: str | None
    decision_reason: str | None
    profile: str
    expires_at: datetime | None
    decided_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    result: dict[str, Any] | None
    is_error: bool
    created_at: datetime


class PendingApproval(ToolCallOut):
    conversation_title: str | None


class ApproveToolCall(BaseModel):
    args_hash: str = Field(min_length=64, max_length=64)
    # Skip asking again for this tool in this conversation (never overrides a deny).
    remember: bool = False
    # Skip asking for every tool in this conversation, and approve the other calls that
    # are waiting in it now. Profile denials still apply.
    remember_all: bool = False


class DenyToolCall(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ToolInfo(BaseModel):
    name: str
    label: str
    description: str
    risk: str
    source: Literal["builtin", "mcp"]
    available: bool
    unavailable_reason: str | None = None


class ToolProfileOut(BaseModel):
    key: str  # built-in name or "custom:<id>"
    label: str
    description: str | None
    builtin: bool
    base: str
    rules: dict[str, DecisionValue]
    # The effective decision for every tool known to this user.
    decisions: dict[str, DecisionValue]


class ToolProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    base: BuiltinProfile = "standard"
    rules: dict[str, DecisionValue] = Field(default_factory=dict)


class ToolProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    base: BuiltinProfile | None = None
    rules: dict[str, DecisionValue] | None = None


class ToolSettingsOut(BaseModel):
    default_profile: str
    disabled_tools: list[str]
    sandbox: dict[str, Any] | None  # the sandbox's health (mode, limits) or None
    network_enabled: bool
    web_allowlist: list[str]
    limits: dict[str, int]


class ToolSettingsUpdate(BaseModel):
    default_profile: str | None = Field(default=None, max_length=80)
    disabled_tools: list[str] | None = Field(default=None, max_length=500)


class McpServerCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    url: str = Field(min_length=8, max_length=1000)
    enabled: bool = True
    allow_private_network: bool = False
    # HTTP headers sent to the server (e.g. {"Authorization": "Bearer …"}). Stored
    # encrypted; never returned.
    headers: dict[str, str] | None = None

    @field_validator("url")
    @classmethod
    def _http(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("The URL must start with https:// or http://")
        return value


class McpServerUpdate(BaseModel):
    url: str | None = Field(default=None, min_length=8, max_length=1000)
    enabled: bool | None = None
    allow_private_network: bool | None = None
    headers: dict[str, str] | None = None  # replaces; {} removes


class McpServerOut(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    enabled: bool
    allow_private_network: bool
    has_credentials: bool
    tools: list[dict[str, Any]]
    last_error: str | None
    last_checked_at: datetime | None
    created_at: datetime


class ScheduleFields(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=50_000)
    kind: Literal["interval", "daily", "weekly"]
    interval_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 31)
    time_of_day: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    timezone: str = Field(default="UTC", max_length=64)
    model_override: str | None = Field(default=None, max_length=255)
    tool_profile: str | None = Field(default=None, max_length=80)
    project_id: uuid.UUID | None = None
    enabled: bool = True


class ScheduleCreate(ScheduleFields):
    pass


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=50_000)
    kind: Literal["interval", "daily", "weekly"] | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 31)
    time_of_day: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] | None = Field(default=None, max_length=7)
    timezone: str | None = Field(default=None, max_length=64)
    model_override: str | None = Field(default=None, max_length=255)
    tool_profile: str | None = Field(default=None, max_length=80)
    project_id: uuid.UUID | None = None
    enabled: bool | None = None


class ScheduleOut(ScheduleFields):
    id: uuid.UUID
    conversation_id: uuid.UUID | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: uuid.UUID | None
    last_status: str | None
    last_error: str | None
    created_at: datetime


class ActivityItem(BaseModel):
    """A run or background job, for the activity panel."""

    kind: Literal["run", "job"]
    id: uuid.UUID
    status: str
    title: str
    conversation_id: uuid.UUID | None = None
    source: str | None = None  # chat | schedule | file.process | export …
    pending_approvals: int = 0
    tool_calls: int = 0
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancellable: bool = False


class ActivityList(BaseModel):
    items: list[ActivityItem]
