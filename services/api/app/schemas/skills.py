import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SkillVersionBrief(BaseModel):
    version: int
    status: str
    created_by: str
    changelog: str | None
    validation: dict
    test: dict
    created_at: datetime
    published_at: datetime | None


class SkillVersionOut(SkillVersionBrief):
    files: dict[str, str]
    frontmatter: dict
    source_conversation_id: uuid.UUID | None


class SkillOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    status: str
    origin: str
    tags: list[str]
    published_version: int | None
    latest_version: int
    pending_versions: list[int] = Field(default_factory=list)
    use_count: int
    success_count: int
    failure_count: int
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SkillDetail(SkillOut):
    versions: list[SkillVersionBrief]
    current: SkillVersionOut | None


class SkillFiles(BaseModel):
    # SKILL.md plus supporting files by path.
    files: dict[str, str] = Field(min_length=1, max_length=40)
    changelog: str | None = Field(default=None, max_length=2000)


class SkillEventOut(BaseModel):
    id: uuid.UUID
    version: int | None
    kind: str
    outcome: str
    run_id: uuid.UUID | None
    duration_ms: int | None
    detail: dict
    created_at: datetime


class SkillFeedback(BaseModel):
    outcome: Literal["success", "failure", "corrected"]
    note: str = Field(default="", max_length=2000)


class SkillSettings(BaseModel):
    trust: Literal["conservative", "balanced", "autonomous"]
    auto_generate: bool
    pending: int = 0


class SkillSettingsUpdate(BaseModel):
    trust: Literal["conservative", "balanced", "autonomous"] | None = None
    auto_generate: bool | None = None
