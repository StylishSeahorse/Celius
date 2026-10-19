"""Durable background jobs (file processing, exports) with the same claim/lease model as
generation runs (ADR 0005). Handlers register by kind and run in the worker process."""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.models import Job, JobStatus

log = logging.getLogger(__name__)

Handler = Callable[[SessionFactory, Settings, Job], Awaitable[dict[str, Any] | None]]
HANDLERS: dict[str, Handler] = {}


class JobFailed(RuntimeError):
    """A permanent, user-facing failure; the message is shown to the user."""


def job_handler(kind: str):
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return register


def enqueue_job(db: AsyncSession, owner_id: uuid.UUID, kind: str, payload: dict) -> Job:
    job = Job(
        id=uuid.uuid4(), owner_id=owner_id, kind=kind, payload=payload, status=JobStatus.queued
    )
    db.add(job)
    return job


async def claim_next_job(
    factory: SessionFactory, worker_id: str, settings: Settings
) -> uuid.UUID | None:
    async with factory() as db:
        candidate = await db.scalar(
            select(Job.id)
            .where(Job.status == JobStatus.queued)
            .order_by(Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if candidate is None:
            return None
        now = utcnow()
        claimed = await db.execute(
            update(Job)
            .where(Job.id == candidate, Job.status == JobStatus.queued)
            .values(
                status=JobStatus.running,
                worker_id=worker_id,
                attempts=Job.attempts + 1,
                lease_expires_at=now + timedelta(seconds=settings.job_lease_seconds),
                started_at=func.coalesce(Job.started_at, now),
            )
            .returning(Job.id)
        )
        job_id = claimed.scalar()
        await db.commit()
        return job_id


async def run_job(
    job_id: uuid.UUID, *, factory: SessionFactory, settings: Settings, worker_id: str
) -> None:
    async with factory() as db:
        job = await db.get(Job, job_id)
        if job is None or job.status != JobStatus.running or job.worker_id != worker_id:
            return
        db.expunge(job)
    handler = HANDLERS.get(job.kind)
    try:
        if handler is None:
            raise JobFailed(f"No handler for job kind {job.kind!r}")
        result = await handler(factory, settings, job)
        status, error = JobStatus.completed, None
    except JobFailed as exc:
        result, status, error = None, JobStatus.failed, str(exc)
    except Exception:
        log.exception("Job %s (%s) crashed", job_id, job.kind)
        result, status, error = None, JobStatus.failed, "Internal error"
    async with factory() as db:
        await db.execute(
            update(Job)
            .where(Job.id == job_id, Job.worker_id == worker_id, Job.status == JobStatus.running)
            .values(
                status=status,
                result=result,
                error=error,
                finished_at=utcnow(),
                lease_expires_at=None,
            )
        )
        await db.commit()


async def recover_stale_jobs(factory: SessionFactory, settings: Settings) -> int:
    async with factory() as db:
        stale = (
            await db.scalars(
                select(Job)
                .where(Job.status == JobStatus.running, Job.lease_expires_at < utcnow())
                .with_for_update(skip_locked=True)
            )
        ).all()
        for job in stale:
            job.worker_id = None
            job.lease_expires_at = None
            if job.attempts < settings.job_max_attempts:
                job.status = JobStatus.queued
            else:
                job.status = JobStatus.failed
                job.error = "The worker stopped while running this job"
                job.finished_at = utcnow()
        await db.commit()
        return len(stale)
