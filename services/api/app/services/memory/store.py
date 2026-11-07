"""Memories: proposals, review, editing, deletion, and the switches that turn memory off.

Memory is off for a chat when any of these is off: the user's memory setting, the
conversation's, or its project's. Off means: nothing from that chat is indexed or
extracted, and nothing is retrieved into it.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.models import (
    MEMORY_CATEGORIES,
    Conversation,
    Memory,
    MemoryChunk,
    MemoryConflict,
    MemoryStatus,
    Project,
    UserSettings,
)
from app.services.audit import audit
from app.services.memory.embeddings import Embedder, cosine
from app.services.memory.entities import EntityRef, link_entities
from app.services.memory.sensitive import find_secret
from app.services.sync import record_sync

MAX_MEMORY_CHARS = 1000
DUPLICATE_SIMILARITY = 0.92
# Contradiction candidates: about the same subject (this close) but not the same fact.
CONFLICT_SIMILARITY = 0.80
CONFLICT_OVERLAP = 0.4
CORRECTION_REASON = "Correction of an earlier memory"
_WORD = re.compile(r"[\w']+")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_NEGATION = {"not", "no", "never", "don't", "doesn't", "isn't", "won't", "can't", "stopped"}
# Default confidence by source: what the user typed or approved is confirmed; the model
# saving something the user said is nearly so; extraction is an inference until reviewed.
SOURCE_CONFIDENCE = {"user": 1.0, "model": 0.9, "extraction": 0.7}


class SensitiveMemory(ValueError):
    """The text looks like it contains a secret; it is never stored."""

    def __init__(self, kind: str):
        super().__init__(f"This looks like it contains a secret ({kind}); it wasn't remembered")
        self.kind = kind


DEFAULT_EXTRACT_CATEGORIES = ("user", "semantic", "episodic", "procedural")


@dataclass
class MemoryPrefs:
    enabled: bool
    auto_extract: bool
    require_review: bool
    extract_categories: tuple[str, ...] = DEFAULT_EXTRACT_CATEGORIES


def prefs_from(settings: Settings, row: UserSettings | None) -> MemoryPrefs:
    saved = ((row.preferences if row else None) or {}).get("memory") or {}
    categories = saved.get("extract_categories")
    return MemoryPrefs(
        enabled=saved.get("enabled", settings.memory_enabled),
        auto_extract=saved.get("auto_extract", settings.memory_auto_extraction),
        require_review=saved.get("require_review", settings.memory_require_review),
        extract_categories=tuple(c for c in categories if c in MEMORY_CATEGORIES)
        if isinstance(categories, list)
        else DEFAULT_EXTRACT_CATEGORIES,
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
        select(Memory.id).where(
            Memory.owner_id == owner_id,
            Memory.content_hash == digest,
            Memory.status != MemoryStatus.superseded,
        )
    ):
        return True  # same text: active, proposed or already rejected (a superseded fact
        # may become true again)
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
    category: str = "user",
    importance: float | None = None,
    confidence: float | None = None,
    occurred_at: datetime | None = None,
    attributes: dict | None = None,
    entities: list[EntityRef] | None = None,
    replaces: uuid.UUID | None = None,
) -> Memory | None:
    """Create a memory. From the model it is a proposal when review is required.
    Returns None for an empty or duplicate one; raises SensitiveMemory for a secret.

    `replaces`: an existing memory this one corrects. An active correction supersedes it
    at once; a proposed one records the pairing so approving it supersedes the old one.
    Other memories about the same subject that seem to disagree become open conflicts."""
    content = clean(content)
    if len(content) < 3:
        return None
    if kind := find_secret(content):
        audit(db, owner_id, "memory.refused_secret", "user", owner_id, source=source, kind=kind)
        raise SensitiveMemory(kind)
    if category not in MEMORY_CATEGORIES:
        category = "user"
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
        category=category,
        importance=_unit(importance, 0.8 if always_use else 0.5),
        confidence=_unit(confidence, SOURCE_CONFIDENCE.get(source, 0.7)),
        occurred_at=occurred_at,
        attributes=dict(attributes or {}),
    )
    db.add(memory)
    await db.flush()
    if entities:
        await link_entities(db, memory, entities)
    await _record_conflicts(db, memory, vector, model, replaces)
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


def _unit(value: float | None, default: float) -> float:
    if value is None:
        return default
    return min(max(float(value), 0.0), 1.0)


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def _negated(text: str) -> bool:
    return bool(_NEGATION & set(_WORD.findall(text.lower())))


def disagreement(a: str, b: str, similarity: float | None) -> str | None:
    """Why two memories about the same subject might contradict each other, or None.

    Heuristic, cheap and model-free: same subject (embedding similarity or word overlap)
    plus a differing value (numbers, negation, or different remaining words). It errs
    towards asking: a false conflict costs the user one click."""
    words_a, words_b = _words(a), _words(b)
    if not words_a or not words_b:
        return None
    overlap = len(words_a & words_b) / len(words_a | words_b)
    same_subject = (similarity is not None and similarity >= CONFLICT_SIMILARITY) or (
        overlap >= CONFLICT_OVERLAP
    )
    if not same_subject:
        return None
    if _numbers(a) and _numbers(b) and _numbers(a) != _numbers(b):
        return "Same subject, different numbers"
    if _negated(a) != _negated(b):
        return "One says the opposite of the other"
    if (prefix := _functional_prefix(a)) and prefix == _functional_prefix(b):
        return "Same subject, different value"
    return None


# Verbs that take one value at a time ("X runs Y", "X is called Y", "X lives in Y"): two
# memories with the same words up to one of these but different endings disagree. Verbs
# like "likes" or "uses" are left out: a person likes and uses many things at once.
_FUNCTIONAL = {
    "is",
    "are",
    "was",
    "runs",
    "called",
    "named",
    "lives",
    "located",
    "works",
    "hosted",
    "version",
    "moved",
    "uses_as",
}


def _functional_prefix(text: str) -> str | None:
    words = _WORD.findall(text.lower())
    for index, word in enumerate(words[:8]):
        if word in _FUNCTIONAL and index >= 1 and index < len(words) - 1:
            prefix = words[: index + 1]
            # "The user is ..." alone says nothing about which attribute follows.
            if set(prefix) - _GENERIC:
                return " ".join(prefix)
            continue
    return None


_GENERIC = {"the", "a", "an", "user", "user's", "is", "are", "was", "they", "their"}


async def _record_conflicts(
    db: AsyncSession,
    memory: Memory,
    vector: list[float] | None,
    model: str | None,
    replaces: uuid.UUID | None,
) -> None:
    if replaces is not None:
        old = await db.scalar(
            select(Memory).where(
                Memory.id == replaces,
                Memory.owner_id == memory.owner_id,
                Memory.status == MemoryStatus.active,
            )
        )
        if old is not None:
            if memory.status == MemoryStatus.active:
                await supersede(db, old, memory)
            else:
                db.add(
                    MemoryConflict(
                        owner_id=memory.owner_id,
                        memory_id=memory.id,
                        other_id=old.id,
                        reason=CORRECTION_REASON,
                    )
                )
    rows = (
        await db.execute(
            select(Memory.id, Memory.content, Memory.embedding, Memory.embedding_model).where(
                Memory.owner_id == memory.owner_id,
                Memory.status == MemoryStatus.active,
                Memory.id != memory.id,
                Memory.id != replaces if replaces else true(),
            )
        )
    ).all()
    for other_id, content, embedding, row_model in rows:
        similarity = None
        if vector is not None and embedding is not None and row_model == model:
            similarity = cosine(vector, list(embedding))
        reason = disagreement(memory.content, content, similarity)
        if reason:
            db.add(
                MemoryConflict(
                    owner_id=memory.owner_id, memory_id=memory.id, other_id=other_id, reason=reason
                )
            )
    await db.flush()


async def supersede(db: AsyncSession, old: Memory, new: Memory) -> None:
    """`new` replaces `old`: the old one is kept for history but never given again."""
    old.status = MemoryStatus.superseded
    old.superseded_by_id = new.id
    audit(db, old.owner_id, "memory.superseded", "memory", old.id, by=str(new.id))
    record_sync(db, old.owner_id, "memory", old.id)


async def resolve_conflict(db: AsyncSession, conflict: MemoryConflict, decision: str) -> None:
    """keep_new: the newer memory wins; keep_old: the newer one is dropped (rejected if it
    was a proposal, superseded otherwise); keep_both: they don't actually disagree."""
    new = await db.get(Memory, conflict.memory_id)
    old = await db.get(Memory, conflict.other_id)
    if new is None or old is None:
        decision = "keep_both"
    elif decision == "keep_new":
        if new.status == MemoryStatus.proposed:
            new.status, new.reviewed_at = MemoryStatus.active, utcnow()
            new.confidence = 1.0
        await supersede(db, old, new)
    elif decision == "keep_old":
        if new.status == MemoryStatus.proposed:
            await reject(db, new)
        elif new.status == MemoryStatus.active:
            await supersede(db, new, old)
    conflict.status = decision
    conflict.resolved_at = utcnow()
    audit(
        db,
        conflict.owner_id,
        "memory.conflict_resolved",
        "memory",
        conflict.memory_id,
        decision=decision,
    )


async def approve(db: AsyncSession, memory: Memory) -> None:
    """A reviewed proposal becomes an active, confirmed memory. A correction it carried
    supersedes the memory it corrects."""
    memory.status = MemoryStatus.active
    memory.reviewed_at = utcnow()
    memory.confidence = 1.0
    if memory.attributes and "review_reason" in memory.attributes:
        memory.attributes = {k: v for k, v in memory.attributes.items() if k != "review_reason"}
    corrections = (
        await db.scalars(
            select(MemoryConflict).where(
                MemoryConflict.memory_id == memory.id,
                MemoryConflict.status == "open",
                MemoryConflict.reason == CORRECTION_REASON,
            )
        )
    ).all()
    for conflict in corrections:
        await resolve_conflict(db, conflict, "keep_new")


async def set_content(
    db: AsyncSession, memory: Memory, content: str, embedder: Embedder | None
) -> None:
    if kind := find_secret(content):
        raise SensitiveMemory(kind)
    memory.content = clean(content)
    memory.content_hash = content_hash(memory.content)
    memory.embedding, memory.embedding_model = None, None
    if embedder is not None:
        vectors = await embedder.embed([memory.content])
        if vectors:
            memory.embedding, memory.embedding_model = vectors[0], embedder.model


async def reject(db: AsyncSession, memory: Memory) -> None:
    """Keep only a hash, so the same thing isn't proposed again; the text is erased."""
    await db.execute(
        update(MemoryConflict)
        .where(
            (MemoryConflict.memory_id == memory.id) | (MemoryConflict.other_id == memory.id),
            MemoryConflict.status == "open",
        )
        .values(status="keep_old", resolved_at=utcnow())
    )
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
