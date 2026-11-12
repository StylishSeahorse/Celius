"""Remote sessions and commands (ADR C-16).

A session ties a conversation to a machine and a working directory. Everything Central
wants a machine to do is a `RemoteCommand` row; the machine's connection delivers it.
"""

import ntpath
import posixpath
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import (
    TERMINAL_RUN_STATUSES,
    Conversation,
    RemoteAgent,
    RemoteCommand,
    RemoteSession,
    Run,
)
from app.models.remote import ENDED_SESSION_STATUSES
from app.services.audit import audit
from app.services.sync import record_sync

ADAPTER_MODE = {"celius": "native", "claude_code": "external"}


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None or moment.tzinfo:
        return moment
    return moment.replace(tzinfo=utcnow().tzinfo)


# ---------------------------------------------------------------------- machine state


def is_online(agent: RemoteAgent, settings: Settings) -> bool:
    seen = _aware(agent.last_seen_at)
    return (
        agent.connection_id is not None
        and seen is not None
        and seen > utcnow() - timedelta(seconds=settings.remote_heartbeat_seconds * 3)
    )


def agent_state(
    agent: RemoteAgent,
    settings: Settings,
    sessions: list[RemoteSession],
    live: dict[uuid.UUID, str] | None = None,
) -> str:
    """One word for the UI: awaiting_authorization, revoked, needs_attention, busy, idle,
    disconnected (unexpectedly), offline. `live`: live_statuses() of the sessions."""
    if agent.status == "pending":
        return "awaiting_authorization"
    if agent.status == "revoked":
        return "revoked"
    live = live or {}
    states = [live.get(s.id, s.status) for s in sessions if s.status not in ENDED_SESSION_STATUSES]
    if is_online(agent, settings):
        if "awaiting_approval" in states:
            return "needs_attention"
        if "running" in states:
            return "busy"
        return "idle"
    if agent.connection_id is not None or agent.disconnect_reason == "lost":
        # The connection ended without a goodbye (network drop, crash, stale socket).
        return "disconnected"
    return "offline"


def effective_permissions(agent: RemoteAgent, session: RemoteSession | None = None) -> dict:
    """What may run: the machine's own policy ∩ Central's config ∩ the session's limits.
    Central can only narrow what the machine's owner allowed on the machine itself."""
    local = agent.local_policy or {}
    config = agent.config or {}
    limits = (session.permissions if session else None) or {}
    allow_shell = bool(local.get("allow_shell")) and bool(config.get("allow_shell", True))
    allow_write = bool(local.get("allow_write", False)) and bool(config.get("allow_write", True))
    if "allow_shell" in limits:
        allow_shell = allow_shell and bool(limits["allow_shell"])
    if "allow_write" in limits:
        allow_write = allow_write and bool(limits["allow_write"])
    adapters = set(local.get("adapters") or []) | {"celius"}
    adapters &= set(config.get("adapters") or ["celius"])
    approval = limits.get("approval") or config.get("approval") or "always"
    if config.get("approval", "always") == "always":
        approval = "always"  # a session can't loosen the machine's setting
    return {
        "allow_shell": allow_shell,
        "allow_write": allow_write,
        "approval": approval if approval in ("always", "profile") else "always",
        "adapters": sorted(adapters),
        "roots": list(local.get("roots") or []),
        "time_limit_seconds": limits.get("time_limit_seconds"),
    }


# ---------------------------------------------------------------------- lookups


async def owned_agent(db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID) -> RemoteAgent:
    agent = await db.scalar(
        select(RemoteAgent).where(RemoteAgent.id == agent_id, RemoteAgent.owner_id == owner_id)
    )
    if agent is None:
        raise not_found("Machine")
    return agent


async def owned_session(
    db: AsyncSession, owner_id: uuid.UUID, session_id: uuid.UUID, *, for_update: bool = False
) -> RemoteSession:
    query = select(RemoteSession).where(
        RemoteSession.id == session_id, RemoteSession.owner_id == owner_id
    )
    if for_update:
        query = query.with_for_update()
    session = await db.scalar(query)
    if session is None:
        raise not_found("Remote session")
    return session


async def open_session_for(db: AsyncSession, conversation_id: uuid.UUID) -> RemoteSession | None:
    """The conversation's session that is not ended, if any."""
    return await db.scalar(
        select(RemoteSession)
        .where(
            RemoteSession.conversation_id == conversation_id,
            RemoteSession.status.not_in(ENDED_SESSION_STATUSES),
        )
        .order_by(RemoteSession.created_at.desc())
        .limit(1)
    )


async def sessions_by_agent(
    db: AsyncSession, agent_ids: list[uuid.UUID], *, open_only: bool = True
) -> dict[uuid.UUID, list[RemoteSession]]:
    if not agent_ids:
        return {}
    query = select(RemoteSession).where(RemoteSession.agent_id.in_(agent_ids))
    if open_only:
        query = query.where(RemoteSession.status.not_in(ENDED_SESSION_STATUSES))
    grouped: dict[uuid.UUID, list[RemoteSession]] = {}
    for row in (await db.scalars(query.order_by(RemoteSession.created_at.desc()))).all():
        grouped.setdefault(row.agent_id, []).append(row)
    return grouped


# ---------------------------------------------------------------------- commands


def enqueue_command(
    db: AsyncSession,
    agent_id: uuid.UUID,
    kind: str,
    *,
    session_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    tool_call_id: uuid.UUID | None = None,
    payload: dict | None = None,
    expires_at: datetime | None = None,
) -> RemoteCommand:
    command = RemoteCommand(
        id=uuid.uuid4(),
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        kind=kind,
        payload=payload or {},
        status="pending",
        expires_at=expires_at,
    )
    db.add(command)
    return command


def session_payload(session: RemoteSession, agent: RemoteAgent) -> dict:
    return {
        "mode": session.mode,
        "adapter": session.adapter,
        "working_directory": session.working_directory,
        "permissions": effective_permissions(agent, session),
        "external_session_id": session.external_session_id,
        "next_seq": session.last_seq + 1,
    }


# ---------------------------------------------------------------------- lifecycle


def _valid_directory(path: str, os_name: str | None) -> bool:
    module = ntpath if os_name == "windows" else posixpath
    return bool(path) and len(path) <= 1000 and "\x00" not in path and module.isabs(path)


async def open_session(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    agent: RemoteAgent,
    conversation: Conversation,
    adapter: str,
    working_directory: str,
    permissions: dict | None,
    ip_address: str | None = None,
) -> RemoteSession:
    if agent.status != "active":
        raise AppError(409, "agent_not_active", "Authorise this machine first")
    mode = ADAPTER_MODE.get(adapter)
    if mode is None:
        raise AppError(422, "unknown_adapter", f"Unknown agent type {adapter!r}")
    allowed = effective_permissions(agent)
    if adapter not in allowed["adapters"]:
        raise AppError(
            409,
            "adapter_not_allowed",
            f"{adapter} is not enabled on this machine (its policy file and its settings "
            "in Celius must both allow it)",
        )
    if adapter == "claude_code" and not (agent.capabilities or {}).get("claude_code"):
        raise AppError(409, "adapter_unavailable", "Claude Code is not installed on this machine")
    if not _valid_directory(working_directory, agent.os):
        raise AppError(422, "invalid_directory", "Give an absolute folder path on the machine")
    if await open_session_for(db, conversation.id) is not None:
        raise AppError(409, "session_exists", "This conversation already runs on a machine")
    if mode == "external":
        has_messages = await db.scalar(
            select(func.count()).select_from(Run).where(Run.conversation_id == conversation.id)
        )
        if has_messages:
            raise AppError(
                409,
                "conversation_not_empty",
                "An external agent session needs a new conversation",
            )
    max_sessions = int((agent.config or {}).get("max_sessions") or 4)
    open_count = await db.scalar(
        select(func.count())
        .select_from(RemoteSession)
        .where(
            RemoteSession.agent_id == agent.id,
            RemoteSession.status.not_in(ENDED_SESSION_STATUSES),
        )
    )
    if open_count >= max_sessions:
        raise AppError(409, "too_many_sessions", "This machine has its maximum of open sessions")
    session = RemoteSession(
        id=uuid.uuid4(),
        owner_id=owner_id,
        conversation_id=conversation.id,
        agent_id=agent.id,
        mode=mode,
        adapter=adapter,
        working_directory=working_directory,
        status="opening",
        permissions=_clean_permissions(permissions or {}),
    )
    db.add(session)
    await db.flush()
    enqueue_command(
        db,
        agent.id,
        "session.open",
        session_id=session.id,
        payload=session_payload(session, agent),
    )
    audit(
        db,
        owner_id,
        "remote_session.opened",
        "remote_session",
        session.id,
        ip_address,
        agent_id=str(agent.id),
        adapter=adapter,
        working_directory=working_directory,
    )
    record_sync(db, owner_id, "conversation", conversation.id, payload={"remote": True})
    return session


def _clean_permissions(raw: dict) -> dict:
    out: dict = {}
    for key in ("allow_shell", "allow_write"):
        if key in raw:
            out[key] = bool(raw[key])
    if raw.get("approval") in ("always", "profile"):
        out["approval"] = raw["approval"]
    seconds = raw.get("time_limit_seconds")
    if isinstance(seconds, int) and 1 <= seconds <= 24 * 3600:
        out["time_limit_seconds"] = seconds
    return out


async def end_session(
    db: AsyncSession,
    session: RemoteSession,
    status: str,
    detail: str | None,
    *,
    notify_agent: bool = True,
) -> None:
    """Terminate or fail a session; its active run (if any) ends with it."""
    from app.services.runs import request_cancel  # avoid an import cycle

    if session.status in ENDED_SESSION_STATUSES:
        return
    session.status = status
    session.status_detail = (detail or "")[:500] or None
    session.ended_at = utcnow()
    if session.current_run_id:
        run = await db.get(Run, session.current_run_id)
        if run is not None and run.status not in TERMINAL_RUN_STATUSES:
            await request_cancel(db, run)
    # Pending instructions for this session will never be needed.
    await db.execute(
        update(RemoteCommand)
        .where(
            RemoteCommand.session_id == session.id,
            RemoteCommand.status == "pending",
            RemoteCommand.kind != "session.close",
        )
        .values(status="cancelled", error="The session ended", completed_at=utcnow())
    )
    if notify_agent and session.agent_id:
        enqueue_command(
            db, session.agent_id, "session.close", session_id=session.id, payload={"reason": status}
        )
    record_sync(
        db, session.owner_id, "conversation", session.conversation_id, payload={"remote": True}
    )


async def end_agent_sessions(db: AsyncSession, agent: RemoteAgent, detail: str) -> None:
    for session in (await sessions_by_agent(db, [agent.id])).get(agent.id, []):
        await end_session(db, session, "terminated", detail, notify_agent=False)


# ---------------------------------------------------------------------- runs


async def latest_session_for(db: AsyncSession, conversation_id: uuid.UUID) -> RemoteSession | None:
    return await db.scalar(
        select(RemoteSession)
        .where(RemoteSession.conversation_id == conversation_id)
        .order_by(RemoteSession.created_at.desc())
        .limit(1)
    )


async def attach_run(
    db: AsyncSession, conversation_id: uuid.UUID, run: Run, trigger_parts: list, *, reason: str
) -> None:
    """Route a new run of a remote conversation to its machine. A conversation whose
    session ended is not silently moved back to the sandbox (or anywhere else): the user
    opens a session again, on the same or another machine."""
    session = await latest_session_for(db, conversation_id)
    if session is None:
        return
    if session.status in ENDED_SESSION_STATUSES:
        raise AppError(
            409,
            "remote_session_ended",
            "This conversation's session on its machine has ended. Open a session again "
            "(on this or another machine) to continue.",
        )
    if session.mode == "external" and reason != "created":
        raise AppError(
            409,
            "not_supported_for_external_agent",
            "An external agent's history can't be edited or regenerated; send a new message",
        )
    run.execution = "remote" if session.mode == "native" else "external"
    run.remote_session_id = session.id
    session.current_run_id = run.id
    if session.mode == "external" and session.agent_id:
        text = "\n\n".join(p.text for p in trigger_parts if p.type == "text" and p.text)
        names = [p.data.get("filename") for p in trigger_parts if p.type == "file" and p.data]
        if names:
            text += (
                "\n\n[Files attached in Celius are not on this machine: "
                + ", ".join(str(n) for n in names)
                + "]"
            )
        enqueue_command(
            db,
            session.agent_id,
            "session.message",
            session_id=session.id,
            run_id=run.id,
            payload={"text": text},
        )


def live_status(session: RemoteSession, run_status: str | None) -> str:
    """The session's state for display: stored lifecycle state, refined by its run."""
    if session.status in ENDED_SESSION_STATUSES or session.status == "opening":
        return session.status
    if run_status == "awaiting_approval":
        return "awaiting_approval"
    if run_status in ("queued", "running"):
        return "running"
    return "idle"


async def live_statuses(db: AsyncSession, sessions: list[RemoteSession]) -> dict[uuid.UUID, str]:
    run_ids = [s.current_run_id for s in sessions if s.current_run_id]
    statuses: dict = {}
    if run_ids:
        rows = await db.execute(select(Run.id, Run.status).where(Run.id.in_(run_ids)))
        statuses = {run_id: status.value for run_id, status in rows.all()}
    return {s.id: live_status(s, statuses.get(s.current_run_id)) for s in sessions}


async def summaries_for(db: AsyncSession, settings: Settings, conversation_ids: list[uuid.UUID]):
    """The latest remote session of each conversation, for the chat list and header."""
    from app.schemas.remote import RemoteSessionSummary

    if not conversation_ids:
        return {}
    rows = (
        await db.scalars(
            select(RemoteSession)
            .where(RemoteSession.conversation_id.in_(conversation_ids))
            .order_by(RemoteSession.created_at)
        )
    ).all()
    latest = {row.conversation_id: row for row in rows}  # newest wins
    agents = {
        a.id: a
        for a in (
            await db.scalars(
                select(RemoteAgent).where(
                    RemoteAgent.id.in_({s.agent_id for s in latest.values() if s.agent_id})
                )
            )
        ).all()
    }
    live = await live_statuses(db, list(latest.values()))
    out = {}
    for conversation_id, session in latest.items():
        agent = agents.get(session.agent_id)
        out[conversation_id] = RemoteSessionSummary(
            id=session.id,
            agent_id=session.agent_id,
            agent_name=agent.name if agent else None,
            agent_os=agent.os if agent else None,
            agent_online=is_online(agent, settings) if agent else False,
            mode=session.mode,
            adapter=session.adapter,
            working_directory=session.working_directory,
            status=live.get(session.id, session.status),
            status_detail=session.status_detail,
        )
    return out
