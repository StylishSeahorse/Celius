import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.errors import not_found
from app.models import TERMINAL_RUN_STATUSES, Message, MessageStatus, Run, RunEvent, RunStatus
from app.schemas.events import RunEventType
from app.services.sync import record_sync


async def append_event(
    db: AsyncSession, run_id: uuid.UUID, type_: RunEventType, data: dict[str, Any]
) -> RunEvent:
    event = RunEvent(run_id=run_id, type=type_.value, data=data, created_at=utcnow())
    db.add(event)
    await db.flush()
    return event


async def get_owned_run(db: AsyncSession, owner_id: uuid.UUID, run_id: uuid.UUID) -> Run:
    run = await db.scalar(select(Run).where(Run.id == run_id, Run.owner_id == owner_id))
    if run is None:
        raise not_found("Run")
    return run


async def request_cancel(db: AsyncSession, run: Run) -> Run:
    """Cancel immediately if not yet started; otherwise flag it for the worker."""
    if run.status in TERMINAL_RUN_STATUSES:
        return run
    run.cancel_requested = True
    if run.status == RunStatus.queued:
        await finish_cancelled(db, run)
    return run


async def finish_cancelled(db: AsyncSession, run: Run) -> None:
    run.status = RunStatus.cancelled
    run.finished_at = utcnow()
    run.lease_expires_at = None
    if run.output_message_id:
        message = await db.get(Message, run.output_message_id)
        if message is not None:
            message.status = MessageStatus.cancelled
            record_sync(
                db,
                run.owner_id,
                "message",
                message.id,
                payload={"conversation_id": str(run.conversation_id)},
            )
    await append_event(
        db, run.id, RunEventType.run_cancelled, {"message_id": str(run.output_message_id)}
    )
