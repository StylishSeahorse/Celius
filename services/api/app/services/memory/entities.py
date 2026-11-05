"""Entities and relationships between them (ADR-C05): plain PostgreSQL tables, no graph
database. An entity is a named thing memories are about (a project, service, machine,
file, person, tool...). Memories link to the entities they mention; relations connect two
entities and remember which memory they were learned from."""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Memory, MemoryEntity, MemoryEntityLink, MemoryRelation

ENTITY_KINDS = (
    "project",
    "service",
    "machine",
    "file",
    "person",
    "organisation",
    "tool",
    "place",
    "topic",
    "other",
)
MAX_ENTITIES_PER_MEMORY = 8
_WORD = re.compile(r"[\w']+")


def entity_key(name: str) -> str:
    """Case and punctuation don't make a different entity ("Proxmox VE" = "proxmox ve")."""
    return " ".join(_WORD.findall(name.lower()))[:200]


@dataclass(frozen=True)
class EntityRef:
    kind: str
    name: str

    @classmethod
    def parse(cls, value: object) -> "EntityRef | None":
        """From {"kind": ..., "name": ...} or "kind:name" or a bare name (kind other)."""
        if isinstance(value, dict):
            kind, name = str(value.get("kind") or "other"), str(value.get("name") or "")
        elif isinstance(value, str):
            kind, _, name = value.partition(":") if ":" in value else ("other", "", value)
        else:
            return None
        kind = kind.strip().lower()
        name = " ".join(name.split())[:200]
        if not entity_key(name):
            return None
        return cls(kind if kind in ENTITY_KINDS else "other", name)


async def upsert_entity(db: AsyncSession, owner_id: uuid.UUID, ref: EntityRef) -> MemoryEntity:
    key = entity_key(ref.name)
    entity = await db.scalar(
        select(MemoryEntity).where(
            MemoryEntity.owner_id == owner_id,
            MemoryEntity.kind == ref.kind,
            MemoryEntity.key == key,
        )
    )
    if entity is None:
        entity = MemoryEntity(
            id=uuid.uuid4(), owner_id=owner_id, kind=ref.kind, name=ref.name, key=key
        )
        db.add(entity)
        await db.flush()
    return entity


async def link_entities(
    db: AsyncSession, memory: Memory, refs: list[EntityRef]
) -> list[MemoryEntity]:
    linked: list[MemoryEntity] = []
    for ref in refs[:MAX_ENTITIES_PER_MEMORY]:
        entity = await upsert_entity(db, memory.owner_id, ref)
        exists = await db.scalar(
            select(MemoryEntityLink.entity_id).where(
                MemoryEntityLink.memory_id == memory.id, MemoryEntityLink.entity_id == entity.id
            )
        )
        if exists is None:
            db.add(MemoryEntityLink(memory_id=memory.id, entity_id=entity.id))
        linked.append(entity)
    await db.flush()
    return linked


async def add_relation(
    db: AsyncSession,
    owner_id: uuid.UUID,
    source: EntityRef,
    relation: str,
    target: EntityRef,
    memory_id: uuid.UUID | None = None,
) -> MemoryRelation:
    relation = "_".join(relation.lower().split())[:64] or "related_to"
    a = await upsert_entity(db, owner_id, source)
    b = await upsert_entity(db, owner_id, target)
    edge = await db.scalar(
        select(MemoryRelation).where(
            MemoryRelation.source_id == a.id,
            MemoryRelation.relation == relation,
            MemoryRelation.target_id == b.id,
        )
    )
    if edge is None:
        edge = MemoryRelation(
            id=uuid.uuid4(),
            owner_id=owner_id,
            source_id=a.id,
            target_id=b.id,
            relation=relation,
            memory_id=memory_id,
        )
        db.add(edge)
        await db.flush()
    return edge


async def entities_of(db: AsyncSession, memory_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict]]:
    if not memory_ids:
        return {}
    rows = (
        await db.execute(
            select(MemoryEntityLink.memory_id, MemoryEntity)
            .join(MemoryEntity, MemoryEntity.id == MemoryEntityLink.entity_id)
            .where(MemoryEntityLink.memory_id.in_(memory_ids))
        )
    ).all()
    found: dict[uuid.UUID, list[dict]] = {}
    for memory_id, entity in rows:
        found.setdefault(memory_id, []).append(
            {"id": str(entity.id), "kind": entity.kind, "name": entity.name}
        )
    return found


async def prune_orphans(db: AsyncSession, owner_id: uuid.UUID) -> int:
    """Entities no memory and no relation refers to any more."""
    linked = select(MemoryEntityLink.entity_id)
    in_relations = select(MemoryRelation.source_id).union(select(MemoryRelation.target_id))
    result = await db.execute(
        delete(MemoryEntity).where(
            MemoryEntity.owner_id == owner_id,
            MemoryEntity.id.not_in(linked),
            MemoryEntity.id.not_in(in_relations),
        )
    )
    return result.rowcount or 0
