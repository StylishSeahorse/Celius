import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey


class ToolCallStatus(enum.StrEnum):
    pending_approval = "pending_approval"
    approved = "approved"
    denied = "denied"  # by the user
    expired = "expired"  # nobody answered in time
    refused = "refused"  # by policy (deny, unknown tool, limits)
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"  # the worker stopped mid-call; never re-run automatically


# Calls whose outcome is final; the model gets a result for each of these.
FINAL_TOOL_CALL_STATUSES = frozenset(
    {
        ToolCallStatus.denied,
        ToolCallStatus.expired,
        ToolCallStatus.refused,
        ToolCallStatus.completed,
        ToolCallStatus.failed,
        ToolCallStatus.cancelled,
        ToolCallStatus.interrupted,
    }
)


class ToolCall(UUIDPrimaryKey, Timestamps, Base):
    """One tool call proposed by the model, the decision on it and its result (audit trail)."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_owner_id_status", "owner_id", "status"),
        UniqueConstraint("run_id", "call_key"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Position in the run: step (model turn) and index within that turn.
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # "<step>:<position>", unique per run, so a retried step never creates duplicates.
    call_key: Mapped[str] = mapped_column(String(32), nullable=False)
    model_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    # SHA-256 of the canonical JSON of `arguments`; approval must quote it.
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        Enum(ToolCallStatus, name="tool_call_status"), nullable=False
    )
    # policy | user | grant | expiry | limit | system
    decision_source: Mapped[str | None] = mapped_column(String(16))
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    profile: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # What the model was given back, and structured detail for the UI.
    model_content: Mapped[str | None] = deferred(mapped_column(Text))
    result: Mapped[dict | None] = mapped_column(JSONType)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ToolGrant(Timestamps, Base):
    """ "Allow for this conversation": skips the approval prompt for one tool, never a deny."""

    __tablename__ = "tool_grants"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    tool_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )


class ToolProfile(UUIDPrimaryKey, Timestamps, Base):
    """A user-defined profile: a built-in base with per-tool overrides."""

    __tablename__ = "tool_profiles"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    base: Mapped[str] = mapped_column(String(32), nullable=False)
    # tool name -> allow | ask | deny
    rules: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class McpServer(UUIDPrimaryKey, Timestamps, Base):
    """An MCP server (streamable HTTP) registered by an administrator."""

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)  # used in tool names
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Allow private/LAN addresses (e.g. an MCP server on the same network).
    allow_private_network: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Fernet token of a JSON object of HTTP headers (e.g. Authorization).
    credentials: Mapped[str | None] = deferred(mapped_column(Text))
    # Last discovered tools: [{name, description, input_schema}]
    tools: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledTask(UUIDPrimaryKey, Timestamps, Base):
    """A stored prompt that starts a run on a schedule, in its own conversation."""

    __tablename__ = "scheduled_tasks"
    __table_args__ = (Index("ix_scheduled_tasks_enabled_next_run_at", "enabled", "next_run_at"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # interval: every `interval_minutes`; daily: at `time_of_day`; weekly: also on `weekdays`.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    interval_minutes: Mapped[int | None] = mapped_column(Integer)
    time_of_day: Mapped[str | None] = mapped_column(String(5))  # "HH:MM"
    weekdays: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)  # 0=Mon
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    model_override: Mapped[str | None] = mapped_column(String(255))
    tool_profile: Mapped[str | None] = mapped_column(String(80))
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL")
    )
    last_status: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(String(500))
