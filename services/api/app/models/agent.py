"""Structured task state kept outside the model (ADR C-08).

An agent task is the work a conversation is doing across replies: its objective, plan,
blockers, files touched and verification results. It survives a dropped history (the
context budget trims old turns), a stopped reply, a failed reply and a worker restart, and
is given back to the model at the start of every run in that conversation.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey, utcnow

# active     being worked on
# paused     interrupted (step limit, error, stop, worker gave up); resumes on the next run
# blocked    the model recorded a blocker it can't solve itself
# completed  done (verified or not: `verified` says)
# failed     given up as impossible
# abandoned  stopped by the user
AGENT_TASK_STATUSES = ("active", "paused", "blocked", "completed", "failed", "abandoned")
OPEN_TASK_STATUSES = ("active", "paused", "blocked")
STEP_STATUSES = ("pending", "in_progress", "done", "failed", "skipped")


class AgentTask(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (
        Index("ix_agent_tasks_conversation_id_status", "conversation_id", "status"),
        Index("ix_agent_tasks_owner_id_status", "owner_id", "status"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # Why it is paused, blocked or failed (shown to the user and the model).
    status_reason: Mapped[str | None] = mapped_column(String(500))
    # [{"n": 1, "title": "...", "status": "pending", "note": "..."}]
    plan: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    blockers: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)  # [str]
    # [{"path": "...", "action": "written|changed|attached|cloned", "at": iso}]
    files: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # [{"check": "...", "passed": bool, "detail": "...", "at": iso}]
    verifications: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # Short notes the model keeps for itself: decisions, findings, relevant context.
    notes: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)  # [str]
    runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentTaskEvent(UUIDPrimaryKey, Base):
    """History and checkpoints: created, plan changes, steps, blockers, verifications,
    tool failures, and a checkpoint at the end of every run (with how it ended)."""

    __tablename__ = "agent_task_events"
    __table_args__ = (Index("ix_agent_task_events_task_id_created_at", "task_id", "created_at"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
