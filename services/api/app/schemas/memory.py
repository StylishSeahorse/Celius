import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import MemoryStatus

Category = Literal["user", "semantic", "episodic", "procedural"]


class EntityIn(BaseModel):
    kind: str = Field(default="other", max_length=32)
    name: str = Field(min_length=1, max_length=200)


class EntityBrief(BaseModel):
    id: uuid.UUID
    kind: str
    name: str


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
    category: Category = "user"
    importance: float = 0.5
    confidence: float = 1.0
    occurred_at: datetime | None = None
    superseded_by_id: uuid.UUID | None = None
    attributes: dict = Field(default_factory=dict)
    entities: list[EntityBrief] = Field(default_factory=list)
    open_conflicts: int = 0
    created_at: datetime
    updated_at: datetime


class MemoryCreate(BaseModel):
    content: str = Field(min_length=3, max_length=1000)
    always_use: bool = False
    scope: Literal["global", "project"] = "global"
    project_id: uuid.UUID | None = None
    category: Category = "user"
    importance: float | None = Field(default=None, ge=0, le=1)
    occurred_at: datetime | None = None
    entities: list[EntityIn] = Field(default_factory=list, max_length=8)
    replaces: uuid.UUID | None = None  # a memory this one corrects


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=3, max_length=1000)
    always_use: bool | None = None
    scope: Literal["global", "project"] | None = None
    project_id: uuid.UUID | None = None
    category: Category | None = None
    importance: float | None = Field(default=None, ge=0, le=1)
    entities: list[EntityIn] | None = Field(default=None, max_length=8)


class MemorySearchHit(BaseModel):
    memory: MemoryOut
    score: float
    semantic: float | None
    keyword: float


class ConflictOut(BaseModel):
    id: uuid.UUID
    reason: str
    status: str
    memory: MemoryOut  # the newer one
    other: MemoryOut  # the existing one it disagrees with
    created_at: datetime


class ConflictResolve(BaseModel):
    decision: Literal["keep_new", "keep_old", "keep_both"]


class EntityOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str
    description: str | None
    memories: int


class RelationOut(BaseModel):
    id: uuid.UUID
    source: EntityBrief
    relation: str
    target: EntityBrief
    memory_id: uuid.UUID | None


class RelationCreate(BaseModel):
    source: EntityIn
    relation: str = Field(min_length=1, max_length=64)
    target: EntityIn


class EntityDetail(EntityOut):
    memory_items: list[MemoryOut]
    relations: list[RelationOut]


class MemoryApprove(BaseModel):
    # Approve as edited (optional).
    content: str | None = Field(default=None, min_length=3, max_length=1000)


class MemorySettings(BaseModel):
    enabled: bool
    auto_extract: bool
    require_review: bool
    # Which kinds of memory extraction may propose.
    extract_categories: list[Category] = Field(default_factory=list)
    embeddings: bool  # configured on the server (read only)
    counts: dict[str, int] = Field(default_factory=dict)


class MemorySettingsUpdate(BaseModel):
    enabled: bool | None = None
    auto_extract: bool | None = None
    require_review: bool | None = None
    extract_categories: list[Category] | None = None


class ConversationMemory(BaseModel):
    """What a chat has contributed, so deletion can say what it removes."""

    memory_active: bool
    excerpts: int
    memories_learned: int


class ForgetResult(BaseModel):
    memories: int
    excerpts: int
