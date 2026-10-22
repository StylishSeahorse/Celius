import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import MemoryStatus


class MemoryOut(BaseModel):
    id: uuid.UUID
    scope: Literal["global", "project"]
    project_id: uuid.UUID | None
    content: str
    status: MemoryStatus
    source: str
    source_conversation_id: uuid.UUID | None
    source_conversation_title: str | None = None
    last_used_at: datetime | None
    use_count: int
    always_use: bool
    created_at: datetime
    updated_at: datetime


class MemoryCreate(BaseModel):
    content: str = Field(min_length=3, max_length=1000)
    always_use: bool = False
    scope: Literal["global", "project"] = "global"
    project_id: uuid.UUID | None = None


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=3, max_length=1000)
    always_use: bool | None = None
    scope: Literal["global", "project"] | None = None
    project_id: uuid.UUID | None = None


class MemoryApprove(BaseModel):
    # Approve as edited (optional).
    content: str | None = Field(default=None, min_length=3, max_length=1000)


class MemorySettings(BaseModel):
    enabled: bool
    auto_extract: bool
    require_review: bool
    embeddings: bool  # configured on the server (read only)
    counts: dict[str, int] = Field(default_factory=dict)


class MemorySettingsUpdate(BaseModel):
    enabled: bool | None = None
    auto_extract: bool | None = None
    require_review: bool | None = None


class ConversationMemory(BaseModel):
    """What a chat has contributed, so deletion can say what it removes."""

    memory_active: bool
    excerpts: int
    memories_learned: int


class ForgetResult(BaseModel):
    memories: int
    excerpts: int
