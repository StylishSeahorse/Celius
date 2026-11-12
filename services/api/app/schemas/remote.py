"""Celius Remote API shapes (ADR C-16)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AgentState = Literal[
    "awaiting_authorization",
    "revoked",
    "needs_attention",
    "busy",
    "idle",
    "disconnected",
    "offline",
]


# ---------------------------------------------------------------------- user side


class EnrollmentCreate(BaseModel):
    name: str | None = Field(default=None, max_length=100)


class EnrollmentOut(BaseModel):
    code: str  # shown once
    expires_at: datetime
    command: str  # what to run on the machine


class RemoteAgentConfig(BaseModel):
    allow_shell: bool = True
    allow_write: bool = True
    # always: every write/command on the machine asks first; profile: the chat's tool
    # profile decides, as for the sandbox.
    approval: Literal["always", "profile"] = "always"
    adapters: list[Literal["celius", "claude_code"]] = Field(
        default_factory=lambda: ["celius", "claude_code"]
    )
    max_sessions: int = Field(default=4, ge=1, le=32)
    network: Literal["unrestricted", "restricted"] = "unrestricted"


class RemoteAgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    config: RemoteAgentConfig | None = None


class RemoteSessionBrief(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    conversation_title: str | None = None
    mode: str
    adapter: str
    working_directory: str
    status: str
    status_detail: str | None
    created_at: datetime
    ended_at: datetime | None


class RemoteAgentOut(BaseModel):
    id: uuid.UUID
    name: str
    hostname: str | None
    os: str | None
    os_version: str | None
    arch: str | None
    agent_version: str | None
    status: str  # pending | active | revoked
    state: AgentState
    online: bool
    capabilities: dict[str, Any]
    local_policy: dict[str, Any]
    config: dict[str, Any]
    permissions: dict[str, Any]  # effective: machine policy ∩ Central config
    resources: dict[str, Any] | None
    connected_at: datetime | None
    last_seen_at: datetime | None
    disconnected_at: datetime | None
    disconnect_reason: str | None
    authorized_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    active_sessions: list[RemoteSessionBrief] = Field(default_factory=list)


class RemoteAgentList(BaseModel):
    items: list[RemoteAgentOut]


class SessionPermissions(BaseModel):
    allow_shell: bool | None = None
    allow_write: bool | None = None
    approval: Literal["always", "profile"] | None = None
    time_limit_seconds: int | None = Field(default=None, ge=1, le=24 * 3600)


class RemoteSessionCreate(BaseModel):
    agent_id: uuid.UUID
    # An existing conversation, or None to create one.
    conversation_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)
    project_id: uuid.UUID | None = None
    adapter: Literal["celius", "claude_code"] = "celius"
    working_directory: str = Field(min_length=1, max_length=1000)
    permissions: SessionPermissions = Field(default_factory=SessionPermissions)


class RemoteSessionOut(RemoteSessionBrief):
    agent_id: uuid.UUID | None
    agent_name: str | None
    agent_os: str | None
    agent_state: AgentState | None
    agent_online: bool
    permissions: dict[str, Any]
    effective_permissions: dict[str, Any]
    external_session_id: str | None
    current_run_id: uuid.UUID | None
    last_seq: int
    opened_at: datetime | None


class RemoteSessionList(BaseModel):
    items: list[RemoteSessionOut]


class RemoteEventOut(BaseModel):
    seq: int
    type: str
    run_id: uuid.UUID | None
    data: dict[str, Any]
    occurred_at: datetime | None
    created_at: datetime


class RemoteEventPage(BaseModel):
    items: list[RemoteEventOut]
    last_seq: int
    more: bool


# ---------------------------------------------------------------------- agent side


class MachineFacts(BaseModel):
    hostname: str | None = Field(default=None, max_length=255)
    os: Literal["windows", "linux", "macos"] | None = None
    os_version: str | None = Field(default=None, max_length=200)
    arch: str | None = Field(default=None, max_length=32)
    agent_version: str | None = Field(default=None, max_length=32)
    protocol_version: int | None = None


class EnrollRequest(MachineFacts):
    code: str = Field(min_length=4, max_length=40)


class EnrollResponse(BaseModel):
    agent_id: uuid.UUID
    credential: str  # shown once; store it securely
    status: str
    name: str


class TokenRequest(BaseModel):
    credential: str = Field(min_length=10, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    expires_at: int  # Unix time
    agent_id: uuid.UUID
    websocket_path: str = "/api/v1/remote/agent/connect"


class RotateResponse(BaseModel):
    credential: str
