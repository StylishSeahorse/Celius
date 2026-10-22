"""Memories: proposals, review, editing, deletion, and the switches that turn memory off.

Memory is off for a chat when any of these is off: the user's memory setting, the
conversation's, or its project's. Off means: nothing from that chat is indexed or
extracted, and nothing is retrieved into it.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.models import (
    Conversation,
    Memory,
    MemoryChunk,
    MemoryStatus,
    Project,
    UserSettings,
)
from app.services.audit import audit
from app.services.memory.embeddings import Embedder, cosine
from app.services.sync import record_sync

MAX_MEMORY_CHARS = 1000
DUPLICATE_SIMILARITY = 0.92
_WORD = re.compile(r"[\w']+")


@dataclass
class MemoryPrefs:
    enabled: bool
    auto_extract: bool
    require_review: bool


def prefs_from(settings: Settings, row: UserSettings | None) -> MemoryPrefs:
    saved = ((row.preferences if row else None) or {}).get("memory") or {}
    return MemoryPrefs(
        enabled=saved.get("enabled", settings.memory_enabled),
        auto_extract=saved.get("auto_extract", settings.memory_auto_extraction),
        require_review=saved.get("require_review", settings.memory_require_review),
    )


async def user_prefs(db: AsyncSession, owner_id: uuid.UUID, settings: Settings) -> MemoryPrefs:
    return prefs_from(settings, await db.get(UserSettings, owner_id))


async def memory_active_for(
    db: AsyncSession, conversation: Conversation, settings: Settings
) -> bool:
    """Whether this chat may be remembered from and receive memories."""
    if not conversation.memory_enabled or conversation.deleted_at is not None:
        return False
    if not (await user_prefs(db, conversation.owner_id, settings)).enabled:
        return False
    if conversation.project_id:
        project = await db.get(Project, conversation.project_id)
        if project is None or not project.memory_enabled:
            return False
    return True


def normalise(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def content_hash(text: str) -> str:
    return hashlib.sha256(normalise(text).encode()).hexdigest()


def clean(text: str) -> str:
    text = " ".join(text.split())
    return text[:MAX_MEMORY_CHARS]


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


async def _is_duplicate(
    db: AsyncSession, owner_id: uuid.UUID, text: str, vector: list[float] | None, model: str | None
) -> bool:
    digest = content_hash(text)
    if await db.scalar(
        select(Memory.id).where(Memory.owner_id == owner_id, Memory.content_hash == digest)
    ):
        return True  # same text: active, proposed or already rejected
    words = _words(text)
    rows = (
        await db.execute(
            select(Memory.content, Memory.embedding, Memory.embedding_model).where(
                Memory.owner_id == owner_id,
                Memory.status.in_([MemoryStatus.active, MemoryStatus.proposed]),
            )
        )
    ).all()
    for content, embedding, row_model in rows:
        if vector is not None and embedding is not None and row_model == model:
            if cosine(vector, list(embedding)) >= DUPLICATE_SIMILARITY:
                return True
        other = _words(content)
        if words and other and len(words & other) / len(words | other) >= 0.8:
            return True
    return False


async def add_memory(
    db: AsyncSession,
    owner_id: uuid.UUID,
    content: str,
    settings: Settings,
    *,
    source: str,
    scope: str = "global",
    project_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    embedder: Embedder | None = None,
    status: MemoryStatus | None = None,
    always_use: bool = False,
) -> Memory | None:
    """Create a memory. From the model it is a proposal when review is required.
    Returns None for an empty or duplicate one."""
    content = clean(content)
    if len(content) < 3:
        return None
    vector = None
    if embedder is not None:
        vectors = await embedder.embed([content])
        vector = vectors[0] if vectors else None
    model = embedder.model if vector is not None and embedder else None
    if await _is_duplicate(db, owner_id, content, vector, model):
        return None
    if status is None:
        prefs = await user_prefs(db, owner_id, settings)
        status = (
            MemoryStatus.proposed
            if source != "user" and prefs.require_review
            else MemoryStatus.active
        )
    memory = Memory(
        id=uuid.uuid4(),
        owner_id=owner_id,
        scope="project" if project_id and scope == "project" else "global",
        project_id=project_id if scope == "project" else None,
        content=content,
        content_hash=content_hash(content),
        status=status,
        source=source,
        source_conversation_id=conversation_id,
        source_message_id=message_id,
        embedding=vector,
        embedding_model=model,
        reviewed_at=utcnow() if status == MemoryStatus.active and source == "user" else None,
        always_use=always_use,
    )
    db.add(memory)
    await db.flush()
    audit(
        db,
        owner_id,
        f"memory.{status.value}",
        "memory",
        memory.id,
        source=source,
        scope=memory.scope,
    )
    record_sync(db, owner_id, "memory", memory.id)
    return memory


async def set_content(
    db: AsyncSession, memory: Memory, content: str, embedder: Embedder | None
) -> None:
    memory.content = clean(content)
    memory.content_hash = content_hash(memory.content)
    memory.embedding, memory.embedding_model = None, None
    if embedder is not None:
        vectors = await embedder.embed([memory.content])
        if vectors:
            memory.embedding, memory.embedding_model = vectors[0], embedder.model


async def reject(db: AsyncSession, memory: Memory) -> None:
    """Keep only a hash, so the same thing isn't proposed again; the text is erased."""
    memory.status = MemoryStatus.rejected
    memory.content = ""
    memory.embedding = None
    memory.embedding_model = None
    memory.reviewed_at = utcnow()
    audit(db, memory.owner_id, "memory.rejected", "memory", memory.id)
    record_sync(db, memory.owner_id, "memory", memory.id)


async def delete_memory(db: AsyncSession, memory: Memory) -> None:
    """Gone at once, with its embedding: the next retrieval can't find it."""
    owner_id, memory_id = memory.owner_id, memory.id
    await db.execute(delete(Memory).where(Memory.id == memory_id))
    audit(db, owner_id, "memory.deleted", "memory", memory_id)
    record_sync(db, owner_id, "memory", memory_id, operation="delete")


async def forget_conversation(
    db: AsyncSession, conversation: Conversation, *, memories_too: bool
) -> dict[str, int]:
    """Remove what a chat contributed: its recall index always; the memories learned from
    it when asked. Returns counts for the UI."""
    chunks = await db.execute(
        delete(MemoryChunk).where(MemoryChunk.conversation_id == conversation.id)
    )
    removed = 0
    if memories_too:
        ids = (
            await db.scalars(
                select(Memory.id).where(Memory.source_conversation_id == conversation.id)
            )
        ).all()
        for memory_id in ids:
            await db.execute(delete(Memory).where(Memory.id == memory_id))
            record_sync(db, conversation.owner_id, "memory", memory_id, operation="delete")
        removed = len(ids)
    audit(
        db,
        conversation.owner_id,
        "memory.forget_conversation",
        "conversation",
        conversation.id,
        chunks=chunks.rowcount or 0,
        memories=removed,
    )
    return {"chunks": chunks.rowcount or 0, "memories": removed}


async def forget_project(db: AsyncSession, project: Project) -> int:
    """Memory turned off for a project: drop the recall index of its chats."""
    result = await db.execute(delete(MemoryChunk).where(MemoryChunk.project_id == project.id))
    audit(
        db,
        project.owner_id,
        "memory.forget_project",
        "project",
        project.id,
        chunks=result.rowcount or 0,
    )
    return result.rowcount or 0


async def move_chunks(db: AsyncSession, conversation: Conversation) -> None:
    """A chat moved between projects: its excerpts follow it (scope is by project)."""
    await db.execute(
        update(MemoryChunk)
        .where(MemoryChunk.conversation_id == conversation.id)
        .values(project_id=conversation.project_id)
    )
