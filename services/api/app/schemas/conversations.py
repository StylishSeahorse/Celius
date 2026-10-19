import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models import MessageRole, MessageStatus, RunStatus


class ConversationCreate(BaseModel):
    # Optional client-generated id: makes creation idempotent (offline queue, retries).
    id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)
    project_id: uuid.UUID | None = None
    model_override: str | None = Field(default=None, max_length=255)
    # Model thinking: None = model default, True = on, False = off.
    reasoning: bool | None = None


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    project_id: uuid.UUID | None = None
    model_override: str | None = Field(default=None, max_length=255)
    memory_enabled: bool | None = None
    reasoning: bool | None = None
    archived: bool | None = None
    expected_version: int | None = None


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str | None
    project_id: uuid.UUID | None
    model_override: str | None
    memory_enabled: bool
    reasoning: bool | None
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
    # Messages sharing this message's parent (edits / regenerations), oldest first.
    # Includes this message; length 1 means there are no alternatives.
    sibling_ids: list[uuid.UUID]
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
    # Overrides the conversation's reasoning setting for this reply only.
    reasoning: bool | None = None


class MessageSubmit(BaseModel):
    client_message_id: str = Field(min_length=1, max_length=64)
    content: str = Field(default="", max_length=200_000)
    # Uploaded files (see /files/uploads) to attach. They must have finished processing.
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    parent_message_id: uuid.UUID | None = None
    model: str | None = Field(default=None, max_length=255)
    params: GenerationParams = Field(default_factory=GenerationParams)
    expected_version: int | None = None

    @model_validator(mode="after")
    def _has_content(self) -> "MessageSubmit":
        _needs_content(self.content, self.attachment_ids)
        return self


class MessageSubmitResponse(BaseModel):
    message: MessageOut
    assistant_message: MessageOut
    run: RunOut
    conversation_version: int
    duplicate: bool = False


class MessageEdit(BaseModel):
    """Edit a user message: creates an edited copy on a new branch; the original stays."""

    client_message_id: str = Field(min_length=1, max_length=64)
    content: str = Field(default="", max_length=200_000)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    model: str | None = Field(default=None, max_length=255)
    params: GenerationParams = Field(default_factory=GenerationParams)

    @model_validator(mode="after")
    def _has_content(self) -> "MessageEdit":
        _needs_content(self.content, self.attachment_ids)
        return self


class Regenerate(BaseModel):
    """Generate an alternative reply on a new branch; the original reply stays."""

    client_request_id: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=255)
    params: GenerationParams = Field(default_factory=GenerationParams)


class RegenerateResponse(BaseModel):
    assistant_message: MessageOut
    run: RunOut
    conversation_version: int
    duplicate: bool = False


def _needs_content(content: str, attachment_ids: list) -> None:
    if not content.strip() and not attachment_ids:
        raise ValueError("A message needs text or at least one attachment")
