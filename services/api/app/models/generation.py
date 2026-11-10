import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey, utcnow

BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class RunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    awaiting_approval = "awaiting_approval"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.completed, RunStatus.failed, RunStatus.cancelled})


class Run(UUIDPrimaryKey, Timestamps, Base):
    """Durable record of one assistant generation."""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_status_created_at", "status", "created_at"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    trigger_message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    output_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="SET NULL")
    )
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.queued, index=True, nullable=False
    )
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    params: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tool_iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Wall-clock spent in tool calls, across pauses for approval.
    tool_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Worker lease: a run whose lease expires while `running` is recovered by another worker.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict | None] = mapped_column(JSONType)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunSteer(UUIDPrimaryKey, Base):
    """A message the user sent while a reply was still being written (ADR C-16). The run
    reads it before its next model turn: between tool steps, or after an answer it then
    continues. Shown in the reply where it was read."""

    __tablename__ = "run_steers"
    __table_args__ = (Index("ix_run_steers_run_id_status", "run_id", "status"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # The client's id for the submission, so a retried POST doesn't add it twice.
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    step: Mapped[int | None] = mapped_column(Integer)  # the turn that read it
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    """Persisted stream event; `id` is the SSE event id clients resume from."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelCatalogEntry(Timestamps, Base):
    """Last-known capabilities per model, refreshed from the provider."""

    __tablename__ = "model_catalog"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    context_tokens: Mapped[int | None] = mapped_column(Integer)
    capabilities: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Model capability registry (ADR C-09). `capabilities` holds what was detected or
    # verified; the fields below are set by an administrator and win over detection.
    # lite | standard | advanced; None: chosen from the measured context window.
    profile: Mapped[str | None] = mapped_column(String(16))
    # {"tool_calling": bool, "vision": bool, "parallel_tools": bool,
    #  "structured_output": bool, "reasoning_toggle": bool, "context_tokens": int}
    overrides: Mapped[dict] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )
    # {"max_tool_iterations": int, "max_output_tokens": int}
    limits: Mapped[dict] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)
