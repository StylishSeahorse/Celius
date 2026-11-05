import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AccountOut(BaseModel):
    id: uuid.UUID
    kind: str
    provider: str
    name: str
    config: dict
    has_password: bool
    allow_private_network: bool
    enabled: bool
    last_error: str | None
    last_checked_at: datetime | None


class AccountCreate(BaseModel):
    kind: Literal["calendar", "email"]
    provider: Literal["caldav", "imap"]
    name: str = Field(min_length=1, max_length=200)
    config: dict
    password: str | None = Field(default=None, max_length=1000)
    allow_private_network: bool = False


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict | None = None
    password: str | None = Field(default=None, max_length=1000)
    allow_private_network: bool | None = None
    enabled: bool | None = None


class AccountCheck(BaseModel):
    ok: bool
    error: str | None = None


class EventOut(BaseModel):
    id: str
    calendar: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    location: str | None
    description: str | None
    editable: bool
    task_id: str | None


class EventList(BaseModel):
    events: list[EventOut]
    conflicts: list[list[str]]  # pairs of event ids
    problems: list[str]


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    start: str  # YYYY-MM-DD (all day) or an ISO date-time (local unless offset)
    end: str | None = None
    calendar: str | None = None  # an account id; default Celius's own
    location: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)
    task_id: uuid.UUID | None = None


class EventChange(BaseModel):
    id: str = Field(max_length=2000)
    title: str | None = Field(default=None, max_length=300)
    start: str | None = None
    end: str | None = None
    location: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)


class EventRef(BaseModel):
    id: str = Field(max_length=2000)


class EmailSummaryOut(BaseModel):
    id: str
    sender: str
    to: str
    subject: str
    date: datetime | None
    snippet: str
    unread: bool


class EmailOut(EmailSummaryOut):
    cc: str
    body: str
    attachments: list[str]


class EmailCompose(BaseModel):
    account_id: uuid.UUID
    to: list[str] = Field(default_factory=list, max_length=20)
    cc: list[str] = Field(default_factory=list, max_length=20)
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=50_000)
    reply_to: str | None = None


class AutomationOut(BaseModel):
    id: uuid.UUID
    name: str
    enabled: bool
    trigger: dict
    conditions: list
    actions: list
    next_check_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    consecutive_failures: int
    disabled_reason: str | None
    created_by: str
    created_at: datetime


class AutomationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    trigger: dict
    conditions: list[dict] = Field(default_factory=list, max_length=10)
    actions: list[dict] = Field(min_length=1, max_length=5)


class AutomationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    trigger: dict | None = None
    conditions: list[dict] | None = Field(default=None, max_length=10)
    actions: list[dict] | None = Field(default=None, min_length=1, max_length=5)
    enabled: bool | None = None


class AutomationRunOut(BaseModel):
    id: uuid.UUID
    status: str
    reason: str
    detail: dict
    error: str | None
    created_at: datetime
