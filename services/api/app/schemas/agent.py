import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    n: int
    title: str
    status: str
    note: str | None = None


class AgentTaskOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    conversation_title: str | None = None
    objective: str
    status: str
    status_reason: str | None
    plan: list[PlanStep]
    blockers: list[str]
    files: list[dict]
    verifications: list[dict]
    notes: list[str]
    verified: bool | None  # latest result of each check passed; None: nothing checked
    runs: int
    tool_calls: int
    failed_tool_calls: int
    last_run_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class AgentTaskEventOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID | None
    kind: str
    summary: str
    detail: dict
    created_at: datetime


class AgentTaskDetail(AgentTaskOut):
    events: list[AgentTaskEventOut]


class AgentTaskUpdate(BaseModel):
    status: Literal["active", "paused", "completed", "abandoned"] | None = None
    objective: str | None = Field(default=None, min_length=3, max_length=2000)


class AgentTaskResumed(BaseModel):
    conversation_id: uuid.UUID
    run_id: uuid.UUID | None
