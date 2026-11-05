import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey

# pgvector on PostgreSQL (searched in SQL); a JSON list elsewhere (SQLite in tests).
# No fixed dimension: it depends on the embedding model, which is recorded per row.
EmbeddingType = JSON().with_variant(Vector(), "postgresql")


class MemoryStatus(enum.StrEnum):
    proposed = "proposed"  # waiting in the review queue; never used
    active = "active"
    rejected = "rejected"  # text erased; only a hash is kept so it isn't proposed again
    superseded = "superseded"  # replaced by a newer memory (kept for history, never given)


class MemoryCategory(enum.StrEnum):
    """What kind of knowledge a memory holds (ADR-C02)."""

    user = "user"  # who the user is, preferences, working habits, long-term goals
    semantic = "semantic"  # lasting facts: infrastructure, architecture, decisions
    episodic = "episodic"  # something that happened: a fix, a decision, a finished task
    procedural = "procedural"  # how to do something; full procedures live in skills


MEMORY_CATEGORIES = tuple(c.value for c in MemoryCategory)


class Memory(UUIDPrimaryKey, Timestamps, Base):
    """A durable fact for future chats. Deleting one removes the row (and its embedding)."""

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_owner_id_status", "owner_id", "status"),
        Index("ix_memories_owner_id_category", "owner_id", "category"),
    )

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
    category: Mapped[str] = mapped_column(
        String(16), default="user", server_default="user", nullable=False
    )
    # 0..1: how much this matters when ranking (set by the user or the extractor).
    importance: Mapped[float] = mapped_column(
        Float, default=0.5, server_default="0.5", nullable=False
    )
    # 0..1: 1 = confirmed (the user said it or approved it); lower = an inference.
    confidence: Mapped[float] = mapped_column(
        Float, default=1.0, server_default="1.0", nullable=False
    )
    # When an episodic memory happened (defaults to created_at when absent).
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("memories.id", ondelete="SET NULL")
    )
    # Free-form metadata for filters: {"tags": [...], "skill": "name", ...}
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


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


class MemoryConflict(UUIDPrimaryKey, Timestamps, Base):
    """Two memories that appear to say different things about the same subject.

    Found when a memory is added (store.find_conflicts). Open conflicts are shown to the
    user; resolving one supersedes the losing memory, never silently.
    """

    __tablename__ = "memory_conflicts"
    __table_args__ = (Index("ix_memory_conflicts_owner_id_status", "owner_id", "status"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    memory_id: Mapped[uuid.UUID] = mapped_column(  # the newer memory
        Uuid, ForeignKey("memories.id", ondelete="CASCADE"), index=True, nullable=False
    )
    other_id: Mapped[uuid.UUID] = mapped_column(  # the existing memory it contradicts
        Uuid, ForeignKey("memories.id", ondelete="CASCADE"), index=True, nullable=False
    )
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    # open | keep_new | keep_old | keep_both
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryEntity(UUIDPrimaryKey, Timestamps, Base):
    """A thing memories are about: a project, service, machine, file, person, tool..."""

    __tablename__ = "memory_entities"
    __table_args__ = (
        Index("uq_memory_entities_owner_kind_key", "owner_id", "kind", "key", unique=True),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)  # normalised name
    description: Mapped[str | None] = mapped_column(Text)


class MemoryEntityLink(Base):
    """Which entities a memory mentions."""

    __tablename__ = "memory_entity_links"

    memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memory_entities.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class MemoryRelation(UUIDPrimaryKey, Timestamps, Base):
    """entity --relation--> entity, e.g. celius-api --runs_on--> proxmox-lxc."""

    __tablename__ = "memory_relations"
    __table_args__ = (
        Index(
            "uq_memory_relations_edge",
            "source_id",
            "relation",
            "target_id",
            unique=True,
        ),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memory_entities.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memory_entities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    # The memory this edge was learned from (provenance); kept if that memory goes.
    memory_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("memories.id", ondelete="SET NULL")
    )
