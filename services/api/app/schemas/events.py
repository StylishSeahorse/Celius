"""Run event contract. Mirrors packages/contracts/schemas/run-event.schema.json."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class RunEventType(StrEnum):
    run_started = "run.started"
    message_delta = "message.delta"
    tool_call_proposed = "tool_call.proposed"
    tool_call_awaiting_approval = "tool_call.awaiting_approval"
    tool_call_decided = "tool_call.decided"
    tool_call_started = "tool_call.started"
    tool_call_completed = "tool_call.completed"
    run_progress = "run.progress"
    run_completed = "run.completed"
    run_failed = "run.failed"
    run_cancelled = "run.cancelled"


class RunEventEnvelope(BaseModel):
    event_id: int
    run_id: uuid.UUID
    type: RunEventType
    created_at: datetime
    data: dict[str, Any]
