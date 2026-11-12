"""Celius Remote (ADR C-16): registered machines, their sessions, commands and events.

Central is the source of truth. Agents connect out over a WebSocket; everything they are
asked to do is a `RemoteCommand` row, and everything they report is a `RemoteSessionEvent`
row with a per-session sequence number (unique, so a resent event is stored once).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey

BigIntPK = BigInteger().with_variant(Integer, "sqlite")

AGENT_STATUSES = ("pending", "active", "revoked")
SESSION_MODES = ("native", "external")
SESSION_ADAPTERS = ("celius", "claude_code")
# opening: waiting for the agent to accept the working directory.
SESSION_STATUSES = ("opening", "idle", "running", "awaiting_approval", "failed", "terminated")
ENDED_SESSION_STATUSES = frozenset({"failed", "terminated"})
COMMAND_STATUSES = (
    "pending",
    "delivered",
    "running",
    "completed",
    "failed",
    "cancelled",
    "expired",
)
FINAL_COMMAND_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})


class RemoteAgent(UUIDPrimaryKey, Timestamps, Base):
    """A machine registered by a user. Not trusted until `authorized_at` is set."""

    __tablename__ = "remote_agents"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255))
    os: Mapped[str | None] = mapped_column(String(32))  # windows | linux | macos
    os_version: Mapped[str | None] = mapped_column(String(200))
    arch: Mapped[str | None] = mapped_column(String(32))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    # pending (awaiting the user's authorisation) | active | revoked
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # What the machine reports: shells, python, adapters available, GPUs...
    capabilities: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    # The machine's own policy file (roots, allow_shell, allow_write, adapters), reported in
    # `hello`. Central can only narrow it, never widen it.
    local_policy: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    # Central's policy for this machine (set by the user in Celius).
    config: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    resources: Mapped[dict | None] = mapped_column(JSONType)
    connection_id: Mapped[str | None] = mapped_column(String(64))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # clean | lost | superseded | revoked | server
    disconnect_reason: Mapped[str | None] = mapped_column(String(32))
    last_ip: Mapped[str | None] = mapped_column(String(64))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RemoteAgentCredential(UUIDPrimaryKey, Base):
    """A machine's long-lived secret. Only its SHA-256 is stored."""

    __tablename__ = "remote_agent_credentials"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("remote_agents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set on the previous credential when it is rotated: valid until then (grace period).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RemoteEnrollment(UUIDPrimaryKey, Base):
    """A one-time pairing code a user creates to register a machine."""

    __tablename__ = "remote_enrollments"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("remote_agents.id", ondelete="SET NULL")
    )


class RemoteSession(UUIDPrimaryKey, Timestamps, Base):
    """A conversation's execution on a machine (one open session per conversation)."""

    __tablename__ = "remote_sessions"
    __table_args__ = (Index("ix_remote_sessions_agent_id_status", "agent_id", "status"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("remote_agents.id", ondelete="SET NULL")
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)  # native | external
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)  # celius | claude_code
    working_directory: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="opening", nullable=False)
    status_detail: Mapped[str | None] = mapped_column(String(500))
    # Session-level limits within the machine's: {"allow_shell", "allow_write",
    # "approval", "time_limit_seconds"}.
    permissions: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    external_session_id: Mapped[str | None] = mapped_column(String(128))
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL", use_alter=True)
    )
    # Highest contiguous event sequence number stored.
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RemoteSessionEvent(Base):
    """Durable agent → Central event. (session_id, seq) is unique: a resent event is
    stored, and acted on, once."""

    __tablename__ = "remote_session_events"
    __table_args__ = (
        UniqueConstraint("session_id", "seq"),
        Index("ix_remote_session_events_type_created_at", "type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("remote_sessions.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("remote_agents.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    data: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RemoteCommand(UUIDPrimaryKey, Timestamps, Base):
    """Durable Central → agent instruction. Delivered at least once; the agent records
    each id before acting, so it is executed at most once."""

    __tablename__ = "remote_commands"
    __table_args__ = (Index("ix_remote_commands_agent_id_status", "agent_id", "status"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("remote_agents.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("remote_sessions.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
