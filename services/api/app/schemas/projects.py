import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    id: uuid.UUID | None = None  # client-generated id makes creation idempotent
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, max_length=50_000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, max_length=50_000)
    archived: bool | None = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    instructions: str | None
    archived: bool
    conversation_count: int = 0
    created_at: datetime
    updated_at: datetime
