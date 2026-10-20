import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    id: uuid.UUID | None = None  # client-generated id makes creation idempotent
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, max_length=50_000)
    memory_enabled: bool = True


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, max_length=50_000)
    memory_enabled: bool | None = None
    archived: bool | None = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    instructions: str | None
    memory_enabled: bool = True
    archived: bool
    conversation_count: int = 0
    created_at: datetime
    updated_at: datetime
    # Sharing (A-04): your role, and the owner's name when it isn't you.
    role: str = "owner"
    owner: str | None = None
    shared: bool = False


class MemberIn(BaseModel):
    role: str = Field(pattern="^(read|write)$")


class MemberOut(BaseModel):
    username: str
    display_name: str | None = None
    role: str
