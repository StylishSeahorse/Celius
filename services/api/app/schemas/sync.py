import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SyncChange(BaseModel):
    cursor: int
    entity_type: Literal["conversation", "message", "project", "file"]
    entity_id: uuid.UUID
    operation: Literal["upsert", "delete"]
    payload: dict[str, Any] | None
    created_at: datetime


class SyncChanges(BaseModel):
    changes: list[SyncChange]
    # Pass this back as `cursor` next time. Equals the request cursor when nothing changed.
    cursor: int
    has_more: bool


class SyncCursor(BaseModel):
    cursor: int


class SyncAck(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    cursor: int = Field(ge=0)
