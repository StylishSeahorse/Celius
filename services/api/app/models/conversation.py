import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey


class Project(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "projects"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    instructions: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "conversations"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(300))
    model_override: Mapped[str | None] = mapped_column(String(255))
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Model "thinking": None = model default, True = on, False = off.
    reasoning: Mapped[bool | None] = mapped_column(Boolean)
    # Tool profile name (built-in or "custom:<id>"); None = the user's default.
    tool_profile: Mapped[str | None] = mapped_column(String(80))
    active_branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Optimistic concurrency: bumped on every durable change, checked on writes.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Branch(UUIDPrimaryKey, Timestamps, Base):
    """A line of conversation. Non-root branches fork from an existing message."""

    __tablename__ = "branches"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    fork_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE", use_alter=True)
    )
    head_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="SET NULL", use_alter=True)
    )
    name: Mapped[str | None] = mapped_column(String(200))


class MessageRole(enum.StrEnum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class MessageStatus(enum.StrEnum):
    pending = "pending"
    streaming = "streaming"
    complete = "complete"
    failed = "failed"
    cancelled = "cancelled"


class Message(UUIDPrimaryKey, Timestamps, Base):
    """Immutable message identity. Content lives in revisions/parts."""

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("owner_id", "client_message_id"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("branches.id", ondelete="CASCADE"), index=True, nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="message_role"), nullable=False
    )
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status"), default=MessageStatus.complete, nullable=False
    )
    client_message_id: Mapped[str | None] = mapped_column(String(64))
    current_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(255))


class MessageRevision(Timestamps, Base):
    __tablename__ = "message_revisions"

    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    reason: Mapped[str | None] = mapped_column(String(64))  # created | edited | regenerated


class MessagePart(UUIDPrimaryKey, Base):
    __tablename__ = "message_parts"
    __table_args__ = (UniqueConstraint("message_id", "revision", "position"),)

    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # text | image | file | tool_call | tool_result
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSONType)
