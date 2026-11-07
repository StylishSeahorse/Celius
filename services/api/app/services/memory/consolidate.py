"""Periodic memory upkeep (ADR C-12). No model involved, nothing deleted.

- Likely duplicates (very close in meaning or words, same kind) that slipped past the
  checks when they were added become conflicts "Possibly the same fact": the user keeps
  one (the other is superseded, kept for history) or both. A pair once resolved is never
  raised again.
- Stale inferences (never confirmed, never used, older than MEMORY_STALE_DAYS) go back to
  the review queue with the reason, instead of lingering unseen. Approving one confirms it.

Runs per user every MEMORY_CONSOLIDATION_DAYS (0 turns it off), queued by the worker.
"""

import logging
import uuid
from datetime import timedelta
from itertools import combinations

from sqlalchemy import or_, select
from sqlalchemy.orm import undefer

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.models import Job, JobStatus, Memory, MemoryConflict, MemoryStatus, UserSettings
from app.services.audit import audit
from app.services.jobs import enqueue_job, job_handler
from app.services.memory.embeddings import cosine
from app.services.memory.store import _words, user_prefs
from app.services.sync import record_sync

log = logging.getLogger(__name__)

DUPLICATE_REASON = "Possibly the same fact"
DUPLICATE_SIMILARITY = 0.85
DUPLICATE_OVERLAP = 0.6
MAX_MEMORIES = 2000
STALE_REASON = "Never confirmed and never used: still true?"


def _aware(value):
    from datetime import UTC

    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


async def consolidate(db, owner_id: uuid.UUID, settings: Settings) -> dict:
    memories = (
        await db.scalars(
            select(Memory)
            .options(undefer(Memory.embedding))
            .where(Memory.owner_id == owner_id, Memory.status == MemoryStatus.active)
            .order_by(Memory.created_at)
            .limit(MAX_MEMORIES)
        )
    ).all()
    known_pairs = {
        frozenset((a, b))
        for a, b in (
            await db.execute(
                select(MemoryConflict.memory_id, MemoryConflict.other_id).where(
                    MemoryConflict.owner_id == owner_id
                )
            )
        ).all()
    }
    duplicates = 0
    words = {m.id: _words(m.content) for m in memories}
    for older, newer in combinations(memories, 2):
        if older.category != newer.category or older.project_id != newer.project_id:
            continue
        if frozenset((older.id, newer.id)) in known_pairs:
            continue
        similar = (
            older.embedding is not None
            and newer.embedding is not None
            and older.embedding_model == newer.embedding_model
            and cosine(list(older.embedding), list(newer.embedding)) >= DUPLICATE_SIMILARITY
        )
        a, b = words[older.id], words[newer.id]
        overlap = len(a & b) / len(a | b) if a and b else 0
        if similar or overlap >= DUPLICATE_OVERLAP:
            db.add(
                MemoryConflict(
                    owner_id=owner_id,
                    memory_id=newer.id,
                    other_id=older.id,
                    reason=DUPLICATE_REASON,
                )
            )
            known_pairs.add(frozenset((older.id, newer.id)))
            duplicates += 1

    stale = 0
    if settings.memory_stale_days > 0:
        cutoff = utcnow() - timedelta(days=settings.memory_stale_days)
        for memory in memories:
            if (
                memory.confidence < 0.8
                and memory.use_count == 0
                and not memory.always_use
                and _aware(memory.created_at) < cutoff
            ):
                memory.status = MemoryStatus.proposed
                memory.attributes = {**(memory.attributes or {}), "review_reason": STALE_REASON}
                record_sync(db, owner_id, "memory", memory.id)
                stale += 1
    if duplicates or stale:
        audit(
            db,
            owner_id,
            "memory.consolidated",
            "user",
            owner_id,
            duplicates=duplicates,
            stale=stale,
        )
    return {"duplicates": duplicates, "stale": stale, "checked": len(memories)}


@job_handler("memory.consolidate")
async def consolidate_job(factory: SessionFactory, settings: Settings, job: Job) -> dict:
    async with factory() as db:
        if not (await user_prefs(db, job.owner_id, settings)).enabled:
            return {"skipped": "memory off"}
        result = await consolidate(db, job.owner_id, settings)
        row = await db.get(UserSettings, job.owner_id)
        if row is None:
            row = UserSettings(user_id=job.owner_id, preferences={})
            db.add(row)
        memory = dict(((row.preferences or {}).get("memory")) or {})
        memory["consolidated_at"] = utcnow().isoformat()
        row.preferences = {**(row.preferences or {}), "memory": memory}
        await db.commit()
    return result


async def queue_due(factory: SessionFactory, settings: Settings) -> int:
    """From the worker's maintenance: a consolidation for each user whose last one is
    older than MEMORY_CONSOLIDATION_DAYS (and who has memories)."""
    if settings.memory_consolidation_days <= 0:
        return 0
    from datetime import datetime

    cutoff = utcnow() - timedelta(days=settings.memory_consolidation_days)
    queued = 0
    async with factory() as db:
        owners = (
            await db.scalars(
                select(Memory.owner_id).where(Memory.status == MemoryStatus.active).distinct()
            )
        ).all()
        for owner_id in owners:
            row = await db.get(UserSettings, owner_id)
            last = (
                ((row.preferences if row else None) or {}).get("memory", {}).get("consolidated_at")
            )
            if last and _aware(datetime.fromisoformat(last)) > cutoff:
                continue
            waiting = await db.scalar(
                select(Job.id).where(
                    Job.owner_id == owner_id,
                    Job.kind == "memory.consolidate",
                    or_(Job.status == JobStatus.queued, Job.status == JobStatus.running),
                )
            )
            if waiting is None:
                enqueue_job(db, owner_id, "memory.consolidate", {})
                queued += 1
        await db.commit()
    return queued
