import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.errors import not_found
from app.models import (
    TERMINAL_RUN_STATUSES,
    Message,
    MessageStatus,
    Run,
    RunEvent,
    RunStatus,
    ToolCall,
    ToolCallStatus,
)
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
    if run.status in (RunStatus.queued, RunStatus.awaiting_approval):
        await finish_cancelled(db, run)
    return run


async def finish_cancelled(db: AsyncSession, run: Run) -> None:
    # Calls still waiting for a decision (or approved but not started) never run now.
    waiting = (
        await db.scalars(
            select(ToolCall).where(
                ToolCall.run_id == run.id,
                ToolCall.status.in_([ToolCallStatus.pending_approval, ToolCallStatus.approved]),
            )
        )
    ).all()
    for call in waiting:
        call.status = ToolCallStatus.cancelled
        call.decided_at = call.decided_at or utcnow()
        call.decision_source = call.decision_source or "system"
        call.decision_reason = "The reply was stopped"
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


async def prune_stream_events(factory, older_than_minutes: float) -> int:
    """Delete the token-by-token `message.delta` events of runs that finished a while ago.

    They are only needed to replay a reply while it streams; a finished reply is saved on
    its message, and a client that connects after the end reloads the conversation when it
    sees the terminal event (which, like every other event type, is kept).
    """
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    finished = select(Run.id).where(
        Run.status.in_(TERMINAL_RUN_STATUSES),
        Run.finished_at.is_not(None),
        Run.finished_at < cutoff,
    )
    async with factory() as db:
        result = await db.execute(
            delete(RunEvent).where(
                RunEvent.type == RunEventType.message_delta.value, RunEvent.run_id.in_(finished)
            )
        )
        await db.commit()
    return result.rowcount or 0
