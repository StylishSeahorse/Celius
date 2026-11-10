"""Steering: messages sent while a reply is still being written (ADR C-16).

The API stores them on the run; the worker reads them before its next model turn
(worker/executor.py). Both lock the run row first, so a message either reaches the run
or is refused because the reply has already finished (the client then sends it as a
normal message): nothing is lost between the two.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, not_found
from app.models import TERMINAL_RUN_STATUSES, Run, RunStatus, RunSteer

MAX_CHARS = 4000
MAX_PENDING = 5
MAX_PER_RUN = 20


async def _locked_run(db: AsyncSession, owner_id: uuid.UUID, run_id: uuid.UUID) -> Run:
    run = await db.scalar(
        select(Run).where(Run.id == run_id, Run.owner_id == owner_id).with_for_update()
    )
    if run is None:
        raise not_found("Run")
    return run


async def add(
    db: AsyncSession, owner_id: uuid.UUID, run_id: uuid.UUID, content: str, client_id: str
) -> tuple[RunSteer, bool]:
    """The steer, and whether it was new (False: the same client_id was sent before)."""
    run = await _locked_run(db, owner_id, run_id)
    existing = await db.scalar(
        select(RunSteer).where(RunSteer.run_id == run.id, RunSteer.client_id == client_id)
    )
    if existing is not None:
        return existing, False
    if run.status in TERMINAL_RUN_STATUSES or run.cancel_requested:
        raise AppError(
            409, "run_finished", "The reply has already finished; send it as a new message"
        )
    content = content.strip()
    if not content:
        raise AppError(422, "empty", "Write something")
    counts = dict(
        (
            await db.execute(
                select(RunSteer.status, func.count())
                .where(RunSteer.run_id == run.id)
                .group_by(RunSteer.status)
            )
        ).all()
    )
    if counts.get("pending", 0) >= MAX_PENDING:
        raise AppError(
            409, "too_many_steers", f"{MAX_PENDING} messages are already waiting to be read"
        )
    if sum(counts.values()) >= MAX_PER_RUN:
        raise AppError(
            409, "too_many_steers", "That's enough for one reply: wait for it, or stop it"
        )
    steer = RunSteer(
        id=uuid.uuid4(),
        run_id=run.id,
        owner_id=owner_id,
        content=content[:MAX_CHARS],
        client_id=client_id,
        status="pending",
    )
    db.add(steer)
    await db.flush()
    return steer, True


async def withdraw(
    db: AsyncSession, owner_id: uuid.UUID, run_id: uuid.UUID, steer_id: uuid.UUID
) -> RunSteer:
    run = await _locked_run(db, owner_id, run_id)
    steer = await db.scalar(
        select(RunSteer).where(RunSteer.id == steer_id, RunSteer.run_id == run.id)
    )
    if steer is None:
        raise not_found("Message")
    if steer.status != "pending":
        raise AppError(409, "already_read", "Celius has already read this message")
    steer.status = "withdrawn"
    return steer


async def of_run(db: AsyncSession, owner_id: uuid.UUID, run_id: uuid.UUID) -> list[RunSteer]:
    run = await db.scalar(select(Run).where(Run.id == run_id, Run.owner_id == owner_id))
    if run is None:
        raise not_found("Run")
    return list(
        (
            await db.scalars(
                select(RunSteer).where(RunSteer.run_id == run.id).order_by(RunSteer.created_at)
            )
        ).all()
    )


def waits_for_approval(run: Run) -> bool:
    return run.status == RunStatus.awaiting_approval
