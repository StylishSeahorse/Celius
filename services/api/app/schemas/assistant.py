import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Priority = Literal["low", "normal", "high", "urgent"]
Repeat = Literal["daily", "weekdays", "weekly", "monthly", "yearly"]


class Recurrence(BaseModel):
    freq: Repeat
    interval: int = Field(default=1, ge=1, le=365)


class UserTaskOut(BaseModel):
    id: uuid.UUID
    title: str
    notes: str | None
    status: str
    priority: Priority
    due_at: datetime | None
    due_all_day: bool
    due_date: str | None  # the deadline's day in the user's time zone
    overdue: bool
    remind_at: datetime | None
    reminded_at: datetime | None
    recurrence: Recurrence | None
    project_id: uuid.UUID | None
    parent_id: uuid.UUID | None
    blocked_by: list[uuid.UUID]
    blocked: bool
    tags: list[str]
    attachment_ids: list[uuid.UUID]
    source: str
    source_conversation_id: uuid.UUID | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserTaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    notes: str | None = Field(default=None, max_length=10_000)
    priority: Priority = "normal"
    # "YYYY-MM-DD" (all day) or an ISO date-time; local to the user's time zone.
    due: str | None = Field(default=None, max_length=40)
    remind: str | None = Field(default=None, max_length=40)
    recurrence: Recurrence | None = None
    project_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    blocked_by: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class UserTaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    notes: str | None = Field(default=None, max_length=10_000)
    status: Literal["open", "in_progress", "done", "cancelled"] | None = None
    priority: Priority | None = None
    due: str | None = Field(default=None, max_length=40)  # "" clears it
    remind: str | None = Field(default=None, max_length=40)  # "" clears it
    recurrence: Recurrence | None = None
    clear_recurrence: bool = False
    project_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    blocked_by: list[uuid.UUID] | None = Field(default=None, max_length=20)
    tags: list[str] | None = Field(default=None, max_length=20)
    attachment_ids: list[uuid.UUID] | None = Field(default=None, max_length=20)


class UserTaskChanged(BaseModel):
    task: UserTaskOut
    next_occurrence: UserTaskOut | None = None


class AgentTaskBrief(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    conversation_title: str | None
    objective: str
    status: str


class Briefing(BaseModel):
    date: str
    timezone: str
    overdue: list[UserTaskOut]
    due_today: list[UserTaskOut]
    upcoming: list[UserTaskOut]
    in_progress: list[UserTaskOut]
    plan: list[UserTaskOut]
    suggestions: list[UserTaskOut]
    blocked: list[UserTaskOut]
    agent_tasks: list[AgentTaskBrief]
    waiting: dict[str, int]
    calendar: dict | None
    text: str  # the same, as the model gets it


class AssistantSettings(BaseModel):
    timezone: str
    suggest_tasks: bool
    briefing_time: str | None  # "HH:MM": a daily briefing runs as a scheduled task


class AssistantSettingsUpdate(BaseModel):
    timezone: str | None = Field(default=None, max_length=64)
    suggest_tasks: bool | None = None
    briefing_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^$")
