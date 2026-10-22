"""Memory: the review queue, editing and deletion, and the switches.

Deleting is immediate and complete for what Celius stores: the memory or excerpt and its
embedding are removed in the same transaction, so the next reply can't retrieve them.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import (
    Conversation,
    Memory,
    MemoryChunk,
    MemoryStatus,
    UserSettings,
)
from app.schemas.memory import (
    ConversationMemory,
    ForgetResult,
    MemoryApprove,
    MemoryCreate,
    MemoryOut,
    MemorySettings,
    MemorySettingsUpdate,
    MemoryUpdate,
)
from app.services.access import require_project
from app.services.audit import audit
from app.services.conversations import get_owned_conversation
from app.services.memory import store
from app.services.memory.embeddings import Embedder
from app.services.sync import record_sync

router = APIRouter(tags=["memory"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _out(memory: Memory, titles: dict[uuid.UUID, str] | None = None) -> MemoryOut:
    return MemoryOut(
        id=memory.id,
        scope=memory.scope,
        project_id=memory.project_id,
        content=memory.content,
        status=memory.status,
        source=memory.source,
        source_conversation_id=memory.source_conversation_id,
        source_conversation_title=(titles or {}).get(memory.source_conversation_id),
        last_used_at=memory.last_used_at,
        use_count=memory.use_count,
        always_use=memory.always_use,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


async def _owned(db: AsyncSession, owner_id: uuid.UUID, memory_id: uuid.UUID) -> Memory:
    memory = await db.scalar(
        select(Memory).where(Memory.id == memory_id, Memory.owner_id == owner_id)
    )
    if memory is None or memory.status == MemoryStatus.rejected:
        raise not_found("Memory")
    return memory


async def _check_scope(db, owner_id, scope: str, project_id: uuid.UUID | None) -> None:
    if scope == "project":
        if project_id is None:
            raise AppError(422, "project_required", "A project memory needs a project")
        await require_project(db, owner_id, project_id)  # the memory itself stays yours


async def _with_embedder(settings: Settings):
    return Embedder.from_settings(settings)


@router.get("/memory/settings", response_model=MemorySettings)
async def get_memory_settings(auth: CurrentAuth, db: DB, settings: AppSettings) -> MemorySettings:
    prefs = await store.user_prefs(db, auth.user.id, settings)
    counts = dict(
        (
            await db.execute(
                select(Memory.status, func.count())
                .where(Memory.owner_id == auth.user.id, Memory.status != MemoryStatus.rejected)
                .group_by(Memory.status)
            )
        ).all()
    )
    excerpts = await db.scalar(
        select(func.count()).select_from(MemoryChunk).where(MemoryChunk.owner_id == auth.user.id)
    )
    return MemorySettings(
        enabled=prefs.enabled,
        auto_extract=prefs.auto_extract,
        require_review=prefs.require_review,
        embeddings=settings.model_enable_embeddings,
        counts={
            "active": counts.get(MemoryStatus.active, 0),
            "proposed": counts.get(MemoryStatus.proposed, 0),
            "excerpts": excerpts or 0,
        },
    )


@router.put("/memory/settings", response_model=MemorySettings)
async def update_memory_settings(
    body: MemorySettingsUpdate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> MemorySettings:
    row = await db.get(UserSettings, auth.user.id)
    if row is None:
        row = UserSettings(user_id=auth.user.id, preferences={})
        db.add(row)
    current = dict(((row.preferences or {}).get("memory")) or {})
    current.update(body.model_dump(exclude_none=True))
    row.preferences = {**(row.preferences or {}), "memory": current}
    audit(
        db,
        auth.user.id,
        "memory.settings_changed",
        "user",
        auth.user.id,
        **body.model_dump(exclude_none=True),
    )
    await db.commit()
    return await get_memory_settings(auth, db, settings)


@router.get("/memories", response_model=list[MemoryOut])
async def list_memories(
    auth: CurrentAuth,
    db: DB,
    status_: Annotated[MemoryStatus, Query(alias="status")] = MemoryStatus.active,
    project_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[MemoryOut]:
    if status_ == MemoryStatus.rejected:
        return []  # nothing left to show: their text was erased
    query = select(Memory).where(Memory.owner_id == auth.user.id, Memory.status == status_)
    if project_id:
        query = query.where(Memory.project_id == project_id)
    if q:
        query = query.where(func.lower(Memory.content).like(f"%{q.lower()}%"))
    rows = (await db.scalars(query.order_by(Memory.created_at.desc()).limit(500))).all()
    source_ids = {m.source_conversation_id for m in rows if m.source_conversation_id}
    titles = {}
    if source_ids:
        titles = dict(
            (
                await db.execute(
                    select(Conversation.id, Conversation.title).where(
                        Conversation.id.in_(source_ids), Conversation.deleted_at.is_(None)
                    )
                )
            ).all()
        )
    return [_out(m, titles) for m in rows]


@router.post("/memories", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def create_memory(
    body: MemoryCreate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> MemoryOut:
    await _check_scope(db, auth.user.id, body.scope, body.project_id)
    embedder = await _with_embedder(settings)
    try:
        memory = await store.add_memory(
            db,
            auth.user.id,
            body.content,
            settings,
            source="user",
            scope=body.scope,
            project_id=body.project_id,
            embedder=embedder,
            always_use=body.always_use,
        )
    finally:
        if embedder:
            await embedder.aclose()
    if memory is None:
        raise AppError(409, "duplicate", "Celius already remembers this")
    await db.commit()
    return _out(memory)


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
async def update_memory(
    memory_id: uuid.UUID, body: MemoryUpdate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> MemoryOut:
    memory = await _owned(db, auth.user.id, memory_id)
    if body.scope is not None:
        await _check_scope(db, auth.user.id, body.scope, body.project_id or memory.project_id)
        memory.scope = body.scope
        memory.project_id = (
            (body.project_id or memory.project_id) if body.scope == "project" else None
        )
    if body.always_use is not None:
        memory.always_use = body.always_use
    if body.content is not None and body.content != memory.content:
        embedder = await _with_embedder(settings)
        try:
            await store.set_content(db, memory, body.content, embedder)
        finally:
            if embedder:
                await embedder.aclose()
    audit(db, auth.user.id, "memory.edited", "memory", memory.id)
    record_sync(db, auth.user.id, "memory", memory.id)
    await db.commit()
    return _out(memory)


@router.post("/memories/{memory_id}/approve", response_model=MemoryOut)
async def approve_memory(
    memory_id: uuid.UUID, body: MemoryApprove, auth: CurrentAuth, db: DB, settings: AppSettings
) -> MemoryOut:
    memory = await _owned(db, auth.user.id, memory_id)
    if memory.status != MemoryStatus.proposed:
        raise AppError(409, "not_proposed", "This memory isn't waiting for review")
    if body.content and body.content != memory.content:
        embedder = await _with_embedder(settings)
        try:
            await store.set_content(db, memory, body.content, embedder)
        finally:
            if embedder:
                await embedder.aclose()
    memory.status = MemoryStatus.active
    memory.reviewed_at = utcnow()
    audit(db, auth.user.id, "memory.approved", "memory", memory.id, edited=bool(body.content))
    record_sync(db, auth.user.id, "memory", memory.id)
    await db.commit()
    return _out(memory)


@router.post("/memories/{memory_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_memory(memory_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    memory = await _owned(db, auth.user.id, memory_id)
    if memory.status != MemoryStatus.proposed:
        raise AppError(409, "not_proposed", "This memory isn't waiting for review")
    await store.reject(db, memory)
    await db.commit()


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    memory = await _owned(db, auth.user.id, memory_id)
    await store.delete_memory(db, memory)
    await db.commit()


@router.delete("/memories", response_model=ForgetResult)
async def forget_everything(
    auth: CurrentAuth, db: DB, confirm: Annotated[str, Query()] = ""
) -> ForgetResult:
    """Delete every memory and every recall excerpt (pass confirm=forget-everything)."""
    if confirm != "forget-everything":
        raise AppError(422, "confirm_required", "Pass confirm=forget-everything")
    memories = await db.execute(delete(Memory).where(Memory.owner_id == auth.user.id))
    excerpts = await db.execute(delete(MemoryChunk).where(MemoryChunk.owner_id == auth.user.id))
    result = ForgetResult(memories=memories.rowcount or 0, excerpts=excerpts.rowcount or 0)
    audit(db, auth.user.id, "memory.forget_everything", "user", auth.user.id, **result.model_dump())
    record_sync(db, auth.user.id, "memory", auth.user.id, operation="delete")
    await db.commit()
    return result


@router.get("/conversations/{conversation_id}/memory", response_model=ConversationMemory)
async def conversation_memory(
    conversation_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> ConversationMemory:
    conversation = await get_owned_conversation(db, auth.user.id, conversation_id)
    excerpts = await db.scalar(
        select(func.count())
        .select_from(MemoryChunk)
        .where(MemoryChunk.conversation_id == conversation.id)
    )
    learned = await db.scalar(
        select(func.count())
        .select_from(Memory)
        .where(
            Memory.source_conversation_id == conversation.id,
            Memory.status != MemoryStatus.rejected,
        )
    )
    return ConversationMemory(
        memory_active=await store.memory_active_for(db, conversation, settings),
        excerpts=excerpts or 0,
        memories_learned=learned or 0,
    )
