import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey

# pgvector on PostgreSQL (searched in SQL); a JSON list elsewhere (SQLite in tests).
# No fixed dimension: it depends on the embedding model, which is recorded per row.
EmbeddingType = JSON().with_variant(Vector(), "postgresql")


class MemoryStatus(enum.StrEnum):
    proposed = "proposed"  # waiting in the review queue; never used
    active = "active"
    rejected = "rejected"  # text erased; only a hash is kept so it isn't proposed again


class Memory(UUIDPrimaryKey, Timestamps, Base):
    """A durable fact for future chats. Deleting one removes the row (and its embedding)."""

    __tablename__ = "memories"
    __table_args__ = (Index("ix_memories_owner_id_status", "owner_id", "status"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # global: every chat; project: only chats in `project_id`.
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[MemoryStatus] = mapped_column(
        Enum(MemoryStatus, name="memory_status"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # user | model | extraction
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="SET NULL")
    )
    embedding = deferred(mapped_column(EmbeddingType, nullable=True))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    use_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Given to every chat in scope (who the user is, how they want answers). Otherwise a
    # memory is given only when it is relevant to the message.
    always_use: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )


class MemoryChunk(UUIDPrimaryKey, Base):
    """An excerpt of an earlier chat, for recall in other chats of the same scope."""

    __tablename__ = "memory_chunks"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Copied from the conversation (kept in step when it moves) so scope is one filter.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = deferred(mapped_column(EmbeddingType, nullable=True))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
