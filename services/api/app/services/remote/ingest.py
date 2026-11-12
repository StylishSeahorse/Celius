"""Agent → Central events (ADR C-16): stored once, in order, then acted on.

Each session's events carry a sequence number assigned by the agent. A batch is processed
in order under a lock on the session row; an event at or below the stored `last_seq` is a
resend and is dropped; a gap stops the batch (the agent resends from the ack). Projections
(command results, session state, the external agent's conversation) run only for events
stored by this call, in the same transaction, so nothing is applied twice.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import RemoteAgent, RemoteCommand, RemoteSession, RemoteSessionEvent
from app.models.remote import ENDED_SESSION_STATUSES, FINAL_COMMAND_STATUSES
from app.services.remote import external
from app.services.sync import record_sync

log = logging.getLogger(__name__)

MAX_EVENT_TYPE = 48
MAX_EVENT_BYTES = 1_000_000


@dataclass
class IngestResult:
    session_id: uuid.UUID
    last_seq: int
    stored: int
    duplicates: int
    gap: bool = False
    error: str | None = None


def _when(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _uuid(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


async def ingest_events(
    db: AsyncSession, agent: RemoteAgent, session_id: uuid.UUID, events: list[dict]
) -> IngestResult:
    session = await db.scalar(
        select(RemoteSession).where(RemoteSession.id == session_id).with_for_update()
    )
    if session is None or session.agent_id != agent.id:
        # Never accept events for a session this machine doesn't own.
        return IngestResult(session_id, 0, 0, 0, error="unknown_session")
    stored = duplicates = 0
    gap = False
    ordered = sorted(
        (e for e in events if isinstance(e, dict) and isinstance(e.get("seq"), int)),
        key=lambda e: e["seq"],
    )
    for raw in ordered:
        seq = raw["seq"]
        if seq <= session.last_seq:
            duplicates += 1
            continue
        if seq != session.last_seq + 1:
            gap = True
            break
        type_ = str(raw.get("type") or "")[:MAX_EVENT_TYPE]
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        event = RemoteSessionEvent(
            session_id=session.id,
            agent_id=agent.id,
            run_id=_uuid(raw.get("run_id")),
            seq=seq,
            type=type_,
            data=data,
            occurred_at=_when(raw.get("occurred_at")),
            created_at=utcnow(),
        )
        db.add(event)
        session.last_seq = seq
        stored += 1
        try:
            async with db.begin_nested():
                await _project(db, agent, session, event)
        except Exception:
            # A projection bug must not lose the event or block the stream.
            log.exception("Projection of %s seq %d failed", type_, seq)
    await db.flush()
    return IngestResult(session.id, session.last_seq, stored, duplicates, gap)


async def _project(
    db: AsyncSession, agent: RemoteAgent, session: RemoteSession, event: RemoteSessionEvent
) -> None:
    data = event.data
    if event.type == "session.opened":
        if session.status == "opening":
            session.status = "idle"
            session.opened_at = utcnow()
            session.status_detail = None
            if data.get("working_directory"):
                session.working_directory = str(data["working_directory"])[:1000]
            record_sync(
                db,
                session.owner_id,
                "conversation",
                session.conversation_id,
                payload={"remote": True},
            )
        return
    if event.type in ("session.failed", "session.closed"):
        from app.services.remote.sessions import end_session

        status = "failed" if event.type == "session.failed" else "terminated"
        await end_session(
            db, session, status, str(data.get("reason") or "")[:500] or None, notify_agent=False
        )
        return
    if event.type in ("command.started", "command.completed"):
        await _command_update(db, agent, session, event)
        return
    if session.mode == "external" and session.status not in ENDED_SESSION_STATUSES:
        await external.project(db, session, event)


async def _command_update(
    db: AsyncSession, agent: RemoteAgent, session: RemoteSession, event: RemoteSessionEvent
) -> None:
    command_id = _uuid(event.data.get("command_id"))
    if command_id is None:
        return
    command = await db.get(RemoteCommand, command_id)
    if command is None or command.agent_id != agent.id or command.status in FINAL_COMMAND_STATUSES:
        return  # unknown, someone else's, or already final (e.g. expired, cancelled)
    if event.type == "command.started":
        command.status = "running"
        return
    status = event.data.get("status")
    command.status = status if status in ("completed", "failed", "cancelled") else "failed"
    result = event.data.get("result")
    command.result = result if isinstance(result, dict) else None
    error = event.data.get("error")
    command.error = str(error)[:2000] if error else None
    command.completed_at = utcnow()
