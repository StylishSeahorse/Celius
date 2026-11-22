import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import SessionFactory, get_db, get_session_factory
from app.models import TERMINAL_RUN_STATUSES, Run, RunEvent
from app.schemas.conversations import RunOut
from app.schemas.events import RunEventEnvelope
from app.services import steering
from app.services.conversations import run_out
from app.services.runs import get_owned_run, request_cancel

router = APIRouter(prefix="/runs", tags=["runs"])

DB = Annotated[AsyncSession, Depends(get_db)]
EVENT_BATCH = 500


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, auth: CurrentAuth, db: DB) -> RunOut:
    return run_out(await get_owned_run(db, auth.user.id, run_id))


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: uuid.UUID, auth: CurrentAuth, db: DB) -> RunOut:
    run = await get_owned_run(db, auth.user.id, run_id)
    await request_cancel(db, run)
    await db.commit()
    return run_out(run)


class SteerIn(BaseModel):
    content: str = Field(min_length=1, max_length=steering.MAX_CHARS)
    client_id: str = Field(min_length=8, max_length=64)


class SteerOut(BaseModel):
    id: uuid.UUID
    content: str
    status: Literal["pending", "delivered", "withdrawn"]
    step: int | None
    created_at: datetime
    delivered_at: datetime | None
    # The reply waits for an approval: the message is read once that is answered.
    waits_for_approval: bool = False


def _steer_out(steer, run: Run | None = None) -> SteerOut:
    return SteerOut(
        id=steer.id,
        content=steer.content,
        status=steer.status,
        step=steer.step,
        created_at=steer.created_at,
        delivered_at=steer.delivered_at,
        waits_for_approval=bool(
            run is not None and steer.status == "pending" and steering.waits_for_approval(run)
        ),
    )


@router.post("/{run_id}/steer", response_model=SteerOut, status_code=status.HTTP_201_CREATED)
async def steer_run(
    run_id: uuid.UUID, body: SteerIn, response: Response, auth: CurrentAuth, db: DB
) -> SteerOut:
    """Add a message for the reply being written: it is read before its next model turn.
    409 run_finished when the reply ended first (send it as a normal message then)."""
    steer, new = await steering.add(db, auth.user.id, run_id, body.content, body.client_id)
    run = await db.get(Run, run_id)
    await db.commit()
    if not new:
        response.status_code = status.HTTP_200_OK
    return _steer_out(steer, run)


@router.get("/{run_id}/steers", response_model=list[SteerOut])
async def run_steers(run_id: uuid.UUID, auth: CurrentAuth, db: DB) -> list[SteerOut]:
    run = await get_owned_run(db, auth.user.id, run_id)
    return [_steer_out(s, run) for s in await steering.of_run(db, auth.user.id, run_id)]


@router.delete("/{run_id}/steers/{steer_id}", response_model=SteerOut)
async def withdraw_steer(
    run_id: uuid.UUID, steer_id: uuid.UUID, auth: CurrentAuth, db: DB
) -> SteerOut:
    """Take back a message the reply hasn't read yet."""
    steer = await steering.withdraw(db, auth.user.id, run_id, steer_id)
    await db.commit()
    return _steer_out(steer)


def _format_sse(event: RunEvent) -> str:
    envelope = RunEventEnvelope(
        event_id=event.id,
        run_id=event.run_id,
        type=event.type,
        created_at=event.created_at,
        data=event.data,
    )
    return f"id: {event.id}\nevent: {event.type}\ndata: {envelope.model_dump_json()}\n\n"


async def _event_stream(
    request: Request, factory: SessionFactory, settings: Settings, run_id: uuid.UUID, cursor: int
) -> AsyncIterator[str]:
    # Tell EventSource how long to wait before reconnecting.
    yield "retry: 2000\n\n"
    last_sent = time.monotonic()
    while True:
        # A short-lived session per poll: a long stream must not pin a pooled connection.
        async with factory() as db:
            # Status first: the terminal status and terminal event commit together, so if
            # the run already looks finished here, the query below sees every event.
            status = await db.scalar(select(Run.status).where(Run.id == run_id))
            events = (
                await db.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.id > cursor)
                    .order_by(RunEvent.id)
                    .limit(EVENT_BATCH)
                )
            ).all()
        for event in events:
            cursor = event.id
            yield _format_sse(event)
            last_sent = time.monotonic()
        if len(events) == EVENT_BATCH:
            continue
        if not events and (status is None or status in TERMINAL_RUN_STATUSES):
            return  # every event, including the terminal one, has been delivered
        if await request.is_disconnected():
            return
        if time.monotonic() - last_sent >= settings.sse_keepalive_seconds:
            yield ": keepalive\n\n"
            last_sent = time.monotonic()
        await asyncio.sleep(settings.sse_poll_interval_seconds)


@router.get("/{run_id}/events", response_class=StreamingResponse)
async def run_events(
    run_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    factory: Annotated[SessionFactory, Depends(get_session_factory)],
    settings: Annotated[Settings, Depends(get_settings)],
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-Sent Events for a run. Resume with `Last-Event-ID` (or `?after=`)."""
    await get_owned_run(db, auth.user.id, run_id)
    cursor = after or 0
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    return StreamingResponse(
        _event_stream(request, factory, settings, run_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
