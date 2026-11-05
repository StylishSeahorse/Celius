"""Personal tasks: the user's own to-dos, deadlines and reminders (ADR C-10).

Not to be confused with agent tasks (models/agent.py), which are Celius's record of
multi-step work it is doing in a chat.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey

# suggested  noticed in a chat; waits for the user to accept it (never reminded)
# open, in_progress, done, cancelled
USER_TASK_STATUSES = ("suggested", "open", "in_progress", "done", "cancelled")
ACTIVE_TASK_STATUSES = ("open", "in_progress")
PRIORITIES = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


class UserTask(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "user_tasks"
    __table_args__ = (
        Index("ix_user_tasks_owner_id_status", "owner_id", "status"),
        Index("ix_user_tasks_owner_id_due_at", "owner_id", "due_at"),
        Index("ix_user_tasks_remind_at", "remind_at"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_tasks.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # 0..3
    # A deadline. All-day deadlines are stored as midnight in the user's time zone.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_all_day: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    remind_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {"freq": "daily|weekdays|weekly|monthly|yearly", "interval": 1}
    recurrence: Mapped[dict | None] = mapped_column(JSONType)
    # Tasks that must be done first (ids of the same owner's tasks).
    blocked_by: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    tags: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # Files (ids of the owner's stored files) attached to the task.
    attachment_ids: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
