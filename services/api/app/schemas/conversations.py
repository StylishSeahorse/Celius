import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models import MessageRole, MessageStatus, RunStatus


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    project_id: uuid.UUID | None = None
    model_override: str | None = Field(default=None, max_length=255)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    project_id: uuid.UUID | None = None
    model_override: str | None = Field(default=None, max_length=255)
    memory_enabled: bool | None = None
    archived: bool | None = None
    expected_version: int | None = None


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str | None
    project_id: uuid.UUID | None
    model_override: str | None
    memory_enabled: bool
    active_branch_id: uuid.UUID | None
    version: int
    archived: bool
    created_at: datetime
    updated_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationSummary]
    next_cursor: str | None = None


class MessagePartOut(BaseModel):
    type: str
    text: str | None = None
    data: dict[str, Any] | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    branch_id: uuid.UUID
    parent_id: uuid.UUID | None
    role: MessageRole
    status: MessageStatus
    client_message_id: str | None
    revision: int
    model_id: str | None
    parts: list[MessagePartOut]
    created_at: datetime


class RunOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    trigger_message_id: uuid.UUID
    output_message_id: uuid.UUID | None
    status: RunStatus
    model_id: str
    error_code: str | None
    error_message: str | None
    usage: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]
    active_run: RunOut | None = None


class GenerationParams(BaseModel):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)


class MessageSubmit(BaseModel):
    client_message_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=200_000)
    parent_message_id: uuid.UUID | None = None
    model: str | None = Field(default=None, max_length=255)
    params: GenerationParams = Field(default_factory=GenerationParams)
    expected_version: int | None = None


class MessageSubmitResponse(BaseModel):
    message: MessageOut
    assistant_message: MessageOut
    run: RunOut
    conversation_version: int
    duplicate: bool = False
