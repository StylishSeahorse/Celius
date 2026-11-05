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
from app.errors import AppError, not_found
from app.models import (
    Conversation,
    Memory,
    MemoryChunk,
    MemoryConflict,
    MemoryEntity,
    MemoryEntityLink,
    MemoryRelation,
    MemoryStatus,
    UserSettings,
)
from app.schemas.memory import (
    ConflictOut,
    ConflictResolve,
    ConversationMemory,
    EntityBrief,
    EntityDetail,
    EntityIn,
    EntityOut,
    ForgetResult,
    MemoryApprove,
    MemoryCreate,
    MemoryOut,
    MemorySearchHit,
    MemorySettings,
    MemorySettingsUpdate,
    MemoryUpdate,
    RelationCreate,
    RelationOut,
)
from app.services.access import require_project
from app.services.audit import audit
from app.services.conversations import get_owned_conversation
from app.services.jobs import enqueue_job
from app.services.memory import store
from app.services.memory.embeddings import Embedder
from app.services.memory.entities import (
    EntityRef,
    add_relation,
    entities_of,
    link_entities,
    prune_orphans,
)
from app.services.memory.retrieval import search_memories
from app.services.sync import record_sync

router = APIRouter(tags=["memory"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _out(
    memory: Memory,
    titles: dict[uuid.UUID, str] | None = None,
    entities: dict[uuid.UUID, list[dict]] | None = None,
    conflicts: dict[uuid.UUID, int] | None = None,
) -> MemoryOut:
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
        category=memory.category or "user",
        importance=memory.importance if memory.importance is not None else 0.5,
        confidence=memory.confidence if memory.confidence is not None else 1.0,
        occurred_at=memory.occurred_at,
        superseded_by_id=memory.superseded_by_id,
        attributes=memory.attributes or {},
        entities=[EntityBrief(**e) for e in (entities or {}).get(memory.id, [])],
        open_conflicts=(conflicts or {}).get(memory.id, 0),
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


async def _outs(db: AsyncSession, rows: list[Memory]) -> list[MemoryOut]:
    """Memories for the UI with their source chat, entities and open conflicts."""
    ids = [m.id for m in rows]
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
    conflicts: dict[uuid.UUID, int] = {}
    if ids:
        for column in (MemoryConflict.memory_id, MemoryConflict.other_id):
            for memory_id, count in (
                await db.execute(
                    select(column, func.count())
                    .where(column.in_(ids), MemoryConflict.status == "open")
                    .group_by(column)
                )
            ).all():
                conflicts[memory_id] = conflicts.get(memory_id, 0) + count
    return [_out(m, titles, await entities_of(db, ids), conflicts) for m in rows]


def _refs(items: list[EntityIn]) -> list[EntityRef]:
    return [ref for item in items if (ref := EntityRef.parse(item.model_dump()))]


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
        extract_categories=list(prefs.extract_categories),
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
    category: Annotated[list[str] | None, Query()] = None,
) -> list[MemoryOut]:
    if status_ == MemoryStatus.rejected:
        return []  # nothing left to show: their text was erased
    query = select(Memory).where(Memory.owner_id == auth.user.id, Memory.status == status_)
    if project_id:
        query = query.where(Memory.project_id == project_id)
    if category:
        query = query.where(Memory.category.in_(category))
    if q:
        query = query.where(func.lower(Memory.content).like(f"%{q.lower()}%"))
    rows = (await db.scalars(query.order_by(Memory.created_at.desc()).limit(500))).all()
    return await _outs(db, list(rows))


@router.get("/memories/search", response_model=list[MemorySearchHit])
async def search(
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    q: Annotated[str, Query(min_length=2, max_length=500)],
    category: Annotated[list[str] | None, Query()] = None,
    project_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[MemorySearchHit]:
    """Hybrid search (meaning, words, recency, importance) over active memories."""
    embedder = await _with_embedder(settings)
    try:
        ranked = await search_memories(
            db,
            auth.user.id,
            q,
            settings,
            embedder,
            categories=category,
            project_id=project_id,
            limit=limit,
        )
    finally:
        if embedder:
            await embedder.aclose()
    outs = await _outs(db, [memory for _, memory, _, _ in ranked])
    return [
        MemorySearchHit(memory=out, score=score, semantic=semantic, keyword=round(keyword, 4))
        for out, (score, _, semantic, keyword) in zip(outs, ranked, strict=True)
    ]


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
            category=body.category,
            importance=body.importance,
            occurred_at=body.occurred_at,
            entities=_refs(body.entities),
            replaces=body.replaces,
        )
    except store.SensitiveMemory as exc:
        await db.commit()  # keep the audit record of the refusal
        raise AppError(422, "sensitive", str(exc)) from exc
    finally:
        if embedder:
            await embedder.aclose()
    if memory is None:
        raise AppError(409, "duplicate", "Celius already remembers this")
    await db.commit()
    return (await _outs(db, [memory]))[0]


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
    if body.category is not None:
        memory.category = body.category
    if body.importance is not None:
        memory.importance = body.importance
    if body.entities is not None:
        await db.execute(delete(MemoryEntityLink).where(MemoryEntityLink.memory_id == memory.id))
        await link_entities(db, memory, _refs(body.entities))
        await prune_orphans(db, auth.user.id)
    if body.content is not None and body.content != memory.content:
        embedder = await _with_embedder(settings)
        try:
            await store.set_content(db, memory, body.content, embedder)
        except store.SensitiveMemory as exc:
            raise AppError(422, "sensitive", str(exc)) from exc
        finally:
            if embedder:
                await embedder.aclose()
        memory.confidence = 1.0  # the user wrote it
    audit(db, auth.user.id, "memory.edited", "memory", memory.id)
    record_sync(db, auth.user.id, "memory", memory.id)
    await db.commit()
    return (await _outs(db, [memory]))[0]


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
        except store.SensitiveMemory as exc:
            raise AppError(422, "sensitive", str(exc)) from exc
        finally:
            if embedder:
                await embedder.aclose()
    await store.approve(db, memory)
    audit(db, auth.user.id, "memory.approved", "memory", memory.id, edited=bool(body.content))
    record_sync(db, auth.user.id, "memory", memory.id)
    await db.commit()
    return (await _outs(db, [memory]))[0]


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
    await prune_orphans(db, auth.user.id)
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
    await db.execute(delete(MemoryRelation).where(MemoryRelation.owner_id == auth.user.id))
    await db.execute(delete(MemoryEntity).where(MemoryEntity.owner_id == auth.user.id))
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


# ---------------------------------------------------------------------- conflicts


@router.get("/memory/conflicts", response_model=list[ConflictOut])
async def list_conflicts(
    auth: CurrentAuth,
    db: DB,
    status_: Annotated[str, Query(alias="status", pattern="^(open|resolved)$")] = "open",
) -> list[ConflictOut]:
    query = select(MemoryConflict).where(MemoryConflict.owner_id == auth.user.id)
    query = query.where(
        MemoryConflict.status == "open" if status_ == "open" else MemoryConflict.status != "open"
    )
    rows = (await db.scalars(query.order_by(MemoryConflict.created_at.desc()).limit(200))).all()
    found = []
    for conflict in rows:
        pair = [await db.get(Memory, conflict.memory_id), await db.get(Memory, conflict.other_id)]
        if None in pair:
            continue
        new, old = await _outs(db, pair)
        found.append(
            ConflictOut(
                id=conflict.id,
                reason=conflict.reason,
                status=conflict.status,
                memory=new,
                other=old,
                created_at=conflict.created_at,
            )
        )
    return found


@router.post("/memory/conflicts/{conflict_id}/resolve", status_code=status.HTTP_204_NO_CONTENT)
async def resolve_conflict(
    conflict_id: uuid.UUID, body: ConflictResolve, auth: CurrentAuth, db: DB
) -> None:
    conflict = await db.scalar(
        select(MemoryConflict).where(
            MemoryConflict.id == conflict_id, MemoryConflict.owner_id == auth.user.id
        )
    )
    if conflict is None:
        raise not_found("Conflict")
    if conflict.status != "open":
        raise AppError(409, "resolved", "This conflict is already resolved")
    await store.resolve_conflict(db, conflict, body.decision)
    await db.commit()


# ---------------------------------------------------------------------- entities


def _brief(entity: MemoryEntity) -> EntityBrief:
    return EntityBrief(id=entity.id, kind=entity.kind, name=entity.name)


@router.get("/memory/entities", response_model=list[EntityOut])
async def list_entities(
    auth: CurrentAuth, db: DB, kind: Annotated[str | None, Query(max_length=32)] = None
) -> list[EntityOut]:
    counts = (
        select(MemoryEntityLink.entity_id, func.count().label("n"))
        .join(Memory, Memory.id == MemoryEntityLink.memory_id)
        .where(Memory.status == MemoryStatus.active)
        .group_by(MemoryEntityLink.entity_id)
        .subquery()
    )
    query = (
        select(MemoryEntity, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.entity_id == MemoryEntity.id)
        .where(MemoryEntity.owner_id == auth.user.id)
    )
    if kind:
        query = query.where(MemoryEntity.kind == kind)
    rows = (await db.execute(query.order_by(MemoryEntity.name).limit(1000))).all()
    return [
        EntityOut(id=e.id, kind=e.kind, name=e.name, description=e.description, memories=n)
        for e, n in rows
    ]


async def _relations(db: AsyncSession, owner_id: uuid.UUID, entity_id=None) -> list[RelationOut]:
    query = select(MemoryRelation).where(MemoryRelation.owner_id == owner_id)
    if entity_id is not None:
        query = query.where(
            (MemoryRelation.source_id == entity_id) | (MemoryRelation.target_id == entity_id)
        )
    edges = (await db.scalars(query.limit(500))).all()
    ids = {e.source_id for e in edges} | {e.target_id for e in edges}
    entities = {
        e.id: e
        for e in (await db.scalars(select(MemoryEntity).where(MemoryEntity.id.in_(ids)))).all()
    }
    return [
        RelationOut(
            id=e.id,
            source=_brief(entities[e.source_id]),
            relation=e.relation,
            target=_brief(entities[e.target_id]),
            memory_id=e.memory_id,
        )
        for e in edges
    ]


@router.get("/memory/entities/{entity_id}", response_model=EntityDetail)
async def entity_detail(entity_id: uuid.UUID, auth: CurrentAuth, db: DB) -> EntityDetail:
    entity = await db.scalar(
        select(MemoryEntity).where(
            MemoryEntity.id == entity_id, MemoryEntity.owner_id == auth.user.id
        )
    )
    if entity is None:
        raise not_found("Entity")
    memories = (
        await db.scalars(
            select(Memory)
            .join(MemoryEntityLink, MemoryEntityLink.memory_id == Memory.id)
            .where(MemoryEntityLink.entity_id == entity.id, Memory.status == MemoryStatus.active)
            .order_by(Memory.created_at.desc())
        )
    ).all()
    return EntityDetail(
        id=entity.id,
        kind=entity.kind,
        name=entity.name,
        description=entity.description,
        memories=len(memories),
        memory_items=await _outs(db, list(memories)),
        relations=await _relations(db, auth.user.id, entity.id),
    )


@router.get("/memory/relations", response_model=list[RelationOut])
async def list_relations(auth: CurrentAuth, db: DB) -> list[RelationOut]:
    return await _relations(db, auth.user.id)


@router.post("/memory/relations", response_model=RelationOut, status_code=status.HTTP_201_CREATED)
async def create_relation(body: RelationCreate, auth: CurrentAuth, db: DB) -> RelationOut:
    source, target = (
        EntityRef.parse(body.source.model_dump()),
        EntityRef.parse(body.target.model_dump()),
    )
    if source is None or target is None:
        raise AppError(422, "invalid_entity", "Entities need a name")
    edge = await add_relation(db, auth.user.id, source, body.relation, target)
    audit(db, auth.user.id, "memory.relation_added", "memory_relation", edge.id)
    await db.commit()
    return next(r for r in await _relations(db, auth.user.id, edge.source_id) if r.id == edge.id)


@router.delete("/memory/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_relation(relation_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    result = await db.execute(
        delete(MemoryRelation).where(
            MemoryRelation.id == relation_id, MemoryRelation.owner_id == auth.user.id
        )
    )
    if not result.rowcount:
        raise not_found("Relation")
    await prune_orphans(db, auth.user.id)
    await db.commit()


# ---------------------------------------------------------------------- maintenance


@router.post("/memory/reembed", status_code=status.HTTP_202_ACCEPTED)
async def reembed(auth: CurrentAuth, db: DB, settings: AppSettings) -> dict:
    """Re-embed memories and excerpts made with another embedding model (or none), e.g.
    after changing EMBEDDING_MODEL. Until it finishes they are still found by keyword."""
    if not settings.model_enable_embeddings:
        raise AppError(409, "embeddings_off", "Embeddings are not configured on this server")
    job = enqueue_job(db, auth.user.id, "memory.reembed", {})
    await db.commit()
    return {"job_id": str(job.id)}
