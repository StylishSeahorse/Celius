"""Celius Remote (ADR C-16, C-17; docs/remote/ARCHITECTURE.md §7, docs/remote/API.md).

User routes use the normal session auth. Device routes accept only a device code (sign-in),
a machine credential or a machine access token, and only act on that machine and the
conversations that run on it.
"""

import asyncio
import base64
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.install import install_commands
from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import SessionFactory, get_db, get_session_factory
from app.db.base import utcnow
from app.errors import AppError
from app.models import (
    Conversation,
    RemoteAgent,
    RemoteCommand,
    RemoteSession,
    RemoteSessionEvent,
    Run,
    ToolCall,
    User,
)
from app.models.remote import ENDED_SESSION_STATUSES, FINAL_COMMAND_STATUSES
from app.schemas.conversations import ConversationCreate
from app.schemas.remote import (
    DeviceApproval,
    DeviceChatCreate,
    DeviceChatList,
    DeviceChatOut,
    DeviceDecision,
    DeviceDenial,
    DeviceLoginPoll,
    DeviceLoginRequest,
    DeviceLoginResult,
    DeviceLoginStart,
    DeviceMessage,
    DeviceRequestList,
    DeviceRequestOut,
    RemoteAgentList,
    RemoteAgentOut,
    RemoteAgentUpdate,
    RemoteEventOut,
    RemoteEventPage,
    RemoteSessionBrief,
    RemoteSessionCreate,
    RemoteSessionList,
    RemoteSessionOut,
    RotateResponse,
    TokenRequest,
    TokenResponse,
)
from app.services import conversations as conversation_svc
from app.services.audit import audit
from app.services.remote import identity
from app.services.remote import sessions as svc
from app.services.remote.hub import AgentConnection

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Factory = Annotated[SessionFactory, Depends(get_session_factory)]


def _enabled(settings: AppSettings) -> None:
    if not settings.remote_agents_enabled:
        raise AppError(404, "remote_disabled", "Remote machines are turned off on this server")


router = APIRouter(prefix="/remote", tags=["remote"], dependencies=[Depends(_enabled)])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ---------------------------------------------------------------------- serialization


def _brief(
    session: RemoteSession, title: str | None = None, status: str | None = None
) -> RemoteSessionBrief:
    return RemoteSessionBrief(
        id=session.id,
        conversation_id=session.conversation_id,
        conversation_title=title,
        origin=session.origin,
        working_directory=session.working_directory,
        status=status or session.status,
        status_detail=session.status_detail,
        created_at=session.created_at,
        ended_at=session.ended_at,
    )


async def _titles(db: AsyncSession, sessions: list[RemoteSession]) -> dict[uuid.UUID, str | None]:
    ids = {s.conversation_id for s in sessions}
    if not ids:
        return {}
    rows = await db.execute(
        select(Conversation.id, Conversation.title).where(Conversation.id.in_(ids))
    )
    return dict(rows.all())


def agent_out(
    agent: RemoteAgent,
    settings: Settings,
    sessions: list[RemoteSession],
    titles: dict[uuid.UUID, str | None] | None = None,
    live: dict[uuid.UUID, str] | None = None,
) -> RemoteAgentOut:
    titles = titles or {}
    live = live or {}
    return RemoteAgentOut(
        id=agent.id,
        name=agent.name,
        hostname=agent.hostname,
        os=agent.os,
        os_version=agent.os_version,
        arch=agent.arch,
        agent_version=agent.agent_version,
        status=agent.status,
        state=svc.agent_state(agent, settings, sessions, live),
        online=svc.is_online(agent, settings),
        capabilities=agent.capabilities or {},
        local_policy=agent.local_policy or {},
        config=agent.config or {},
        permissions=svc.effective_permissions(agent),
        resources=agent.resources,
        connected_at=agent.connected_at,
        last_seen_at=agent.last_seen_at,
        disconnected_at=agent.disconnected_at,
        disconnect_reason=agent.disconnect_reason,
        authorized_at=agent.authorized_at,
        revoked_at=agent.revoked_at,
        created_at=agent.created_at,
        active_sessions=[
            _brief(s, titles.get(s.conversation_id), live.get(s.id))
            for s in sessions
            if s.status not in ENDED_SESSION_STATUSES
        ],
    )


async def session_out(
    db: AsyncSession, session: RemoteSession, settings: Settings, title: str | None = None
) -> RemoteSessionOut:
    agent = await db.get(RemoteAgent, session.agent_id) if session.agent_id else None
    open_sessions = (await svc.sessions_by_agent(db, [agent.id])).get(agent.id, []) if agent else []
    if title is None:
        title = (await _titles(db, [session])).get(session.conversation_id)
    live = await svc.live_statuses(db, [session, *open_sessions])
    return RemoteSessionOut(
        **_brief(session, title, live.get(session.id)).model_dump(),
        agent_id=session.agent_id,
        agent_name=agent.name if agent else None,
        agent_os=agent.os if agent else None,
        agent_state=svc.agent_state(agent, settings, open_sessions, live) if agent else None,
        agent_online=svc.is_online(agent, settings) if agent else False,
        permissions=session.permissions or {},
        effective_permissions=svc.effective_permissions(agent, session) if agent else {},
        current_run_id=session.current_run_id,
        last_seq=session.last_seq,
        opened_at=session.opened_at,
    )


# ---------------------------------------------------------------------- machines


def _login_command(settings: Settings, username: str | None = None) -> str:
    server = settings.app_public_url.rstrip("/")
    return f"celius login --server {server}" + (f" --user {username}" if username else "")


@router.get("/devices", response_model=DeviceRequestList)
async def device_requests(auth: CurrentAuth, db: DB, settings: AppSettings) -> DeviceRequestList:
    """Devices that ran `celius login --user <you>` and wait for you to accept them."""
    rows = await identity.requests_for(db, auth.user)
    install_cmds = install_commands(settings, auth.user.username)
    return DeviceRequestList(
        items=[
            DeviceRequestOut(
                id=r.id,
                user_code=identity.format_user_code(r.user_code),
                name=r.name,
                hostname=(r.facts or {}).get("hostname"),
                os=(r.facts or {}).get("os"),
                os_version=(r.facts or {}).get("os_version"),
                ip_address=r.ip_address,
                created_at=r.created_at,
                expires_at=r.expires_at,
            )
            for r in rows
        ],
        login_command=_login_command(settings, auth.user.username),
        install_linux=install_cmds["linux"],
        install_windows=install_cmds["windows"],
    )


@router.post("/devices/accept", response_model=RemoteAgentOut, status_code=status.HTTP_201_CREATED)
async def accept_device(
    body: DeviceDecision, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> RemoteAgentOut:
    """Accept a device by the code it shows: it becomes one of your machines."""
    agent = await identity.approve_device(db, auth.user, body.user_code, body.name, _ip(request))
    await db.commit()
    return agent_out(agent, settings, [])


@router.post("/devices/refuse", status_code=status.HTTP_204_NO_CONTENT)
async def refuse_device(
    body: DeviceDecision, request: Request, auth: CurrentAuth, db: DB
) -> Response:
    await identity.deny_device(db, auth.user, body.user_code, _ip(request))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/agents", response_model=RemoteAgentList)
async def list_agents(auth: CurrentAuth, db: DB, settings: AppSettings) -> RemoteAgentList:
    agents = (
        await db.scalars(
            select(RemoteAgent)
            .where(RemoteAgent.owner_id == auth.user.id)
            .order_by(RemoteAgent.created_at)
        )
    ).all()
    grouped = await svc.sessions_by_agent(db, [a.id for a in agents])
    every = [s for group in grouped.values() for s in group]
    titles = await _titles(db, every)
    live = await svc.live_statuses(db, every)
    return RemoteAgentList(
        items=[agent_out(a, settings, grouped.get(a.id, []), titles, live) for a in agents]
    )


@router.get("/agents/{agent_id}", response_model=RemoteAgentOut)
async def get_agent(
    agent_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> RemoteAgentOut:
    agent = await svc.owned_agent(db, auth.user.id, agent_id)
    sessions = (await svc.sessions_by_agent(db, [agent.id])).get(agent.id, [])
    return agent_out(
        agent,
        settings,
        sessions,
        await _titles(db, sessions),
        await svc.live_statuses(db, sessions),
    )


@router.patch("/agents/{agent_id}", response_model=RemoteAgentOut)
async def update_agent(
    agent_id: uuid.UUID,
    body: RemoteAgentUpdate,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
) -> RemoteAgentOut:
    agent = await svc.owned_agent(db, auth.user.id, agent_id)
    changed: dict = {}
    if body.name is not None and body.name != agent.name:
        changed["name"] = body.name
        agent.name = body.name.strip()
    if body.config is not None:
        agent.config = body.config.model_dump()
        changed["config"] = agent.config
    if changed:
        audit(
            db,
            auth.user.id,
            "remote_agent.updated",
            "remote_agent",
            agent.id,
            _ip(request),
            **changed,
        )
        await db.commit()
    return await get_agent(agent_id, auth, db, settings)


async def _ended_with_agent(db: AsyncSession, agent: RemoteAgent, detail: str) -> None:
    await svc.end_agent_sessions(db, agent, detail)


@router.post("/agents/{agent_id}/revoke", response_model=RemoteAgentOut)
async def revoke_agent(
    agent_id: uuid.UUID, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> RemoteAgentOut:
    """Revoke the machine's credentials; its connection closes and its sessions end."""
    agent = await svc.owned_agent(db, auth.user.id, agent_id)
    await identity.revoke(db, agent, auth.user.id, _ip(request))
    await _ended_with_agent(db, agent, "The machine was revoked")
    await db.commit()
    return await get_agent(agent_id, auth, db, settings)


@router.post("/agents/{agent_id}/disconnect", status_code=status.HTTP_202_ACCEPTED)
async def disconnect_agent(
    agent_id: uuid.UUID, request: Request, auth: CurrentAuth, db: DB
) -> dict:
    """Ask the agent to close its connection and reconnect (sessions keep running)."""
    agent = await svc.owned_agent(db, auth.user.id, agent_id)
    svc.enqueue_command(db, agent.id, "agent.disconnect", payload={"reconnect": True})
    audit(
        db,
        auth.user.id,
        "remote_agent.disconnect_requested",
        "remote_agent",
        agent.id,
        _ip(request),
    )
    await db.commit()
    return {"status": "requested"}


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID, request: Request, auth: CurrentAuth, db: DB
) -> Response:
    """Revoke and forget a machine. Its conversations stay."""
    agent = await svc.owned_agent(db, auth.user.id, agent_id)
    if agent.status != "revoked":
        await identity.revoke(db, agent, auth.user.id, _ip(request))
    await _ended_with_agent(db, agent, "The machine was removed")
    audit(db, auth.user.id, "remote_agent.deleted", "remote_agent", agent.id, _ip(request))
    await db.delete(agent)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/agents/{agent_id}/sessions", response_model=RemoteSessionList)
async def agent_sessions(
    agent_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RemoteSessionList:
    """Running sessions first, then the machine's history."""
    agent = await svc.owned_agent(db, auth.user.id, agent_id)
    rows = (
        await db.scalars(
            select(RemoteSession)
            .where(RemoteSession.agent_id == agent.id)
            .order_by(RemoteSession.created_at.desc())
            .limit(limit)
        )
    ).all()
    titles = await _titles(db, list(rows))
    items = [await session_out(db, s, settings, titles.get(s.conversation_id)) for s in rows]
    items.sort(key=lambda s: s.ended_at is not None)
    return RemoteSessionList(items=items)


# ---------------------------------------------------------------------- sessions


@router.post("/sessions", response_model=RemoteSessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: RemoteSessionCreate, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> RemoteSessionOut:
    """Run a conversation on a machine: an existing one, or a new one created here."""
    agent = await svc.owned_agent(db, auth.user.id, body.agent_id)
    if body.conversation_id is not None:
        conversation = await conversation_svc.get_owned_conversation(
            db, auth.user.id, body.conversation_id, for_update=True
        )
        if await conversation_svc.active_run(db, conversation.id) is not None:
            raise AppError(409, "run_in_progress", "Wait for the current response or cancel it")
    else:
        conversation, _ = await conversation_svc.create_conversation(
            db,
            auth.user.id,
            ConversationCreate(title=body.title, project_id=body.project_id),
        )
    session = await svc.open_session(
        db,
        owner_id=auth.user.id,
        agent=agent,
        conversation=conversation,
        working_directory=body.working_directory.strip(),
        permissions=body.permissions.model_dump(exclude_none=True),
        ip_address=_ip(request),
    )
    conversation_svc.touch(db, conversation)
    await db.commit()
    return await session_out(db, session, settings, conversation.title)


@router.get("/sessions", response_model=RemoteSessionList)
async def list_sessions(
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    conversation_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RemoteSessionList:
    query = select(RemoteSession).where(RemoteSession.owner_id == auth.user.id)
    if conversation_id:
        query = query.where(RemoteSession.conversation_id == conversation_id)
    if agent_id:
        query = query.where(RemoteSession.agent_id == agent_id)
    if active is True:
        query = query.where(RemoteSession.status.not_in(ENDED_SESSION_STATUSES))
    elif active is False:
        query = query.where(RemoteSession.status.in_(ENDED_SESSION_STATUSES))
    rows = (await db.scalars(query.order_by(RemoteSession.created_at.desc()).limit(limit))).all()
    titles = await _titles(db, list(rows))
    return RemoteSessionList(
        items=[await session_out(db, s, settings, titles.get(s.conversation_id)) for s in rows]
    )


@router.get("/sessions/{session_id}", response_model=RemoteSessionOut)
async def get_session(
    session_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> RemoteSessionOut:
    return await session_out(db, await svc.owned_session(db, auth.user.id, session_id), settings)


@router.post("/sessions/{session_id}/interrupt", response_model=RemoteSessionOut)
async def interrupt_session(
    session_id: uuid.UUID, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> RemoteSessionOut:
    """Stop what the session is doing now (the current reply); the session stays open."""
    from app.models import Run
    from app.services.runs import request_cancel

    session = await svc.owned_session(db, auth.user.id, session_id, for_update=True)
    if session.status in ENDED_SESSION_STATUSES:
        raise AppError(409, "session_ended", "This session has ended")
    if session.current_run_id:
        run = await db.get(Run, session.current_run_id)
        if run is not None:
            await request_cancel(db, run)
    if session.agent_id:
        svc.enqueue_command(db, session.agent_id, "interrupt", session_id=session.id)
    audit(
        db, auth.user.id, "remote_session.interrupted", "remote_session", session.id, _ip(request)
    )
    await db.commit()
    return await session_out(db, session, settings)


@router.post("/sessions/{session_id}/terminate", response_model=RemoteSessionOut)
async def terminate_session(
    session_id: uuid.UUID, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> RemoteSessionOut:
    """End the session on the machine. The conversation and its history stay."""
    session = await svc.owned_session(db, auth.user.id, session_id, for_update=True)
    await svc.end_session(db, session, "terminated", "Ended by you")
    audit(db, auth.user.id, "remote_session.terminated", "remote_session", session.id, _ip(request))
    await db.commit()
    return await session_out(db, session, settings)


def _event_out(event: RemoteSessionEvent) -> RemoteEventOut:
    return RemoteEventOut(
        seq=event.seq,
        type=event.type,
        run_id=event.run_id,
        data=event.data,
        occurred_at=event.occurred_at,
        created_at=event.created_at,
    )


@router.get("/sessions/{session_id}/events", response_model=RemoteEventPage)
async def session_events(
    session_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    types: Annotated[str | None, Query(max_length=500)] = None,
) -> RemoteEventPage:
    """Replay the session's events after `after` (a sequence number), in order."""
    session = await svc.owned_session(db, auth.user.id, session_id)
    query = select(RemoteSessionEvent).where(
        RemoteSessionEvent.session_id == session.id, RemoteSessionEvent.seq > after
    )
    if types:
        query = query.where(RemoteSessionEvent.type.in_([t for t in types.split(",") if t]))
    rows = (await db.scalars(query.order_by(RemoteSessionEvent.seq).limit(limit + 1))).all()
    return RemoteEventPage(
        items=[_event_out(e) for e in rows[:limit]],
        last_seq=session.last_seq,
        more=len(rows) > limit,
    )


async def _session_stream(
    request: Request,
    factory: SessionFactory,
    settings: Settings,
    session_id: uuid.UUID,
    cursor: int,
) -> AsyncIterator[str]:
    yield "retry: 2000\n\n"
    last_sent = time.monotonic()
    while True:
        async with factory() as db:
            session = await db.get(RemoteSession, session_id)
            rows = (
                await db.scalars(
                    select(RemoteSessionEvent)
                    .where(
                        RemoteSessionEvent.session_id == session_id,
                        RemoteSessionEvent.seq > cursor,
                    )
                    .order_by(RemoteSessionEvent.seq)
                    .limit(500)
                )
            ).all()
        for event in rows:
            cursor = event.seq
            yield (
                f"id: {event.seq}\nevent: {event.type}\n"
                f"data: {_event_out(event).model_dump_json()}\n\n"
            )
            last_sent = time.monotonic()
        if session is not None and not rows:
            # The session's own state changes (status, run) are sent as a snapshot event.
            state = f'{{"status":"{session.status}","last_seq":{session.last_seq}}}'
            if time.monotonic() - last_sent >= settings.sse_keepalive_seconds:
                yield f"event: session.state\ndata: {state}\n\n"
                last_sent = time.monotonic()
        if session is None or (session.status in ENDED_SESSION_STATUSES and not rows):
            return
        if await request.is_disconnected():
            return
        await asyncio.sleep(settings.sse_poll_interval_seconds)


@router.get("/sessions/{session_id}/stream", response_class=StreamingResponse)
async def session_stream(
    session_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    factory: Factory,
    settings: AppSettings,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-Sent Events of the session (terminal output, files, activity). Resume with
    `Last-Event-ID` (the sequence number) or `?after=`."""
    await svc.owned_session(db, auth.user.id, session_id)
    cursor = after or 0
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    return StreamingResponse(
        _session_stream(request, factory, settings, session_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/file")
async def fetch_file(
    session_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    factory: Factory,
    settings: AppSettings,
    path: Annotated[str, Query(min_length=1, max_length=1000)],
) -> Response:
    """Download a file from the session's working directory (read by the agent, which
    applies its own path checks). Waits up to 30 s for the machine."""
    session = await svc.owned_session(db, auth.user.id, session_id)
    if session.status in ENDED_SESSION_STATUSES or session.agent_id is None:
        raise AppError(409, "session_ended", "This session has ended")
    command = svc.enqueue_command(
        db,
        session.agent_id,
        "file.fetch",
        session_id=session.id,
        payload={"path": path, "max_bytes": settings.remote_file_fetch_max_bytes},
        expires_at=utcnow() + timedelta(seconds=30),
    )
    await db.commit()
    command_id = command.id
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        await asyncio.sleep(settings.remote_poll_interval_seconds)
        async with factory() as poll:
            current = await poll.get(RemoteCommand, command_id)
            if current is not None and current.status in FINAL_COMMAND_STATUSES:
                result = current.result or {}
                if (
                    current.status != "completed"
                    or result.get("is_error")
                    or "content_b64" not in result
                ):
                    raise AppError(
                        404 if current.status == "completed" else 504,
                        "file_unavailable",
                        str(
                            result.get("error")
                            or current.error
                            or "The machine could not send this file"
                        ),
                    )
                content = base64.b64decode(result["content_b64"])
                name = str(result.get("name") or "download").replace('"', "")
                return Response(
                    content,
                    media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'},
                )
    raise AppError(504, "machine_timeout", "The machine did not answer in time")


# ---------------------------------------------------------------------- agent side


@router.post("/device/login", response_model=DeviceLoginStart, status_code=status.HTTP_201_CREATED)
async def device_login(
    body: DeviceLoginRequest, request: Request, db: DB, settings: AppSettings
) -> DeviceLoginStart:
    """`celius login` on a device: start a sign-in that someone accepts in Celius."""
    device_code, sign_in = await identity.start_device_login(
        db,
        username=body.username,
        name=body.name,
        facts=body.model_dump(exclude={"username", "name"}),
        ip_address=_ip(request),
        settings=settings,
    )
    await db.commit()
    code = identity.format_user_code(sign_in.user_code)
    return DeviceLoginStart(
        device_code=device_code,
        user_code=code,
        verification_url=f"{settings.app_public_url.rstrip('/')}/#/devices?code={code}",
        expires_in=settings.remote_device_login_ttl_minutes * 60,
        interval=identity.POLL_INTERVAL_SECONDS,
    )


@router.post("/device/login/poll", response_model=DeviceLoginResult)
async def device_login_poll(body: DeviceLoginPoll, db: DB) -> DeviceLoginResult:
    """Has the sign-in been accepted? 428 `authorization_pending` until it is."""
    try:
        agent, credential = await identity.poll_device_login(db, body.device_code)
    except AppError:
        await db.commit()  # an expired request is recorded as such
        raise
    owner = await db.get(User, agent.owner_id)
    await db.commit()
    return DeviceLoginResult(
        agent_id=agent.id, credential=credential, name=agent.name, username=owner.username
    )


@router.post("/token", response_model=TokenResponse)
async def token(body: TokenRequest, db: DB, settings: AppSettings) -> TokenResponse:
    """Trade a machine credential for a short-lived access token."""
    agent, _ = await identity.authenticate_credential(db, body.credential, settings)
    await db.commit()
    if agent.status != "active":
        raise AppError(403, "agent_not_authorized", "Authorise this machine in Celius first")
    access, expires = identity.issue_token(agent.id, settings)
    return TokenResponse(access_token=access, expires_at=expires, agent_id=agent.id)


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value else None


@router.post("/credentials/rotate", response_model=RotateResponse)
async def rotate(request: Request, db: DB, settings: AppSettings) -> RotateResponse:
    """A new credential for the calling machine; the old one keeps working for a grace
    period, or until the new one is first used."""
    agent = await identity.agent_for_token(db, _bearer(request), settings)
    credential = await identity.rotate_credential(db, agent, settings, _ip(request))
    await db.commit()
    return RotateResponse(credential=credential)


# ---------------------------------------------------------------------- the device itself
#
# The `celius` command line on a signed-in device: a chat that runs on this machine, in
# the folder it was started in. It authenticates as the machine (never as a user session)
# and only reaches conversations that run on this machine.


async def _device(request: Request, db: DB, settings: AppSettings) -> tuple[RemoteAgent, User]:
    """The calling machine and its owner, from the machine's access token. A dependency, so
    it is checked before anything else in the request."""
    agent = await identity.agent_for_token(db, _bearer(request), settings)
    owner = await db.get(User, agent.owner_id)
    if owner is None or not owner.is_active:
        raise AppError(403, "owner_inactive", "The account this machine belongs to is disabled")
    return agent, owner


Device = Annotated[tuple[RemoteAgent, User], Depends(_device)]


async def _device_session(
    db: AsyncSession, agent: RemoteAgent, conversation_id: uuid.UUID
) -> RemoteSession:
    session = await db.scalar(
        select(RemoteSession)
        .where(RemoteSession.conversation_id == conversation_id, RemoteSession.agent_id == agent.id)
        .order_by(RemoteSession.created_at.desc())
        .limit(1)
    )
    if session is None:
        raise AppError(404, "not_found", "No chat on this machine with that id")
    return session


async def _device_run(db: AsyncSession, agent: RemoteAgent, run_id: uuid.UUID) -> Run:
    run = await db.get(Run, run_id)
    session = (
        await db.get(RemoteSession, run.remote_session_id)
        if run and run.remote_session_id
        else None
    )
    if run is None or session is None or session.agent_id != agent.id:
        raise AppError(404, "not_found", "Run not found")
    return run


def _device_chat(
    session: RemoteSession, conversation: Conversation, status_: str, settings: Settings
) -> DeviceChatOut:
    return DeviceChatOut(
        conversation_id=conversation.id,
        session_id=session.id,
        title=conversation.title,
        working_directory=session.working_directory,
        status=status_,
        origin=session.origin,
        updated_at=conversation.updated_at,
        url=f"{settings.app_public_url.rstrip('/')}/#/c/{conversation.id}",
    )


@router.post("/device/logout", status_code=status.HTTP_204_NO_CONTENT)
async def device_logout(
    request: Request, device: Device, db: DB, settings: AppSettings
) -> Response:
    """`celius logout`: the machine revokes itself (its sessions end)."""
    agent, owner = device
    await identity.revoke(db, agent, owner.id, _ip(request))
    await svc.end_agent_sessions(db, agent, "The machine signed out")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/device/chats", response_model=DeviceChatOut, status_code=status.HTTP_201_CREATED)
async def device_chat_create(
    body: DeviceChatCreate, request: Request, device: Device, db: DB, settings: AppSettings
) -> DeviceChatOut:
    """A new conversation running on this machine, in `working_directory`."""
    agent, owner = device
    conversation, _ = await conversation_svc.create_conversation(
        db, owner.id, ConversationCreate(title=body.title, model_override=body.model)
    )
    session = await svc.open_session(
        db,
        owner_id=owner.id,
        agent=agent,
        conversation=conversation,
        origin="device",
        working_directory=body.working_directory.strip(),
        permissions={},
        ip_address=_ip(request),
    )
    await db.commit()
    return _device_chat(session, conversation, session.status, settings)


@router.get("/device/chats", response_model=DeviceChatList)
async def device_chats(
    request: Request,
    device: Device,
    db: DB,
    settings: AppSettings,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> DeviceChatList:
    """Recent chats that run on this machine (started here or in Celius)."""
    agent, _ = device
    rows = (
        await db.execute(
            select(RemoteSession, Conversation)
            .join(Conversation, Conversation.id == RemoteSession.conversation_id)
            .where(RemoteSession.agent_id == agent.id, Conversation.deleted_at.is_(None))
            .order_by(Conversation.updated_at.desc())
            .limit(limit * 3)
        )
    ).all()
    seen: set[uuid.UUID] = set()
    latest = []
    for session, conversation in rows:
        if conversation.id not in seen:
            seen.add(conversation.id)
            latest.append((session, conversation))
    latest = latest[:limit]
    live = await svc.live_statuses(db, [s for s, _ in latest])
    return DeviceChatList(
        items=[_device_chat(s, c, live.get(s.id, s.status), settings) for s, c in latest]
    )


@router.get("/device/chats/{conversation_id}")
async def device_chat(
    conversation_id: uuid.UUID, request: Request, device: Device, db: DB, settings: AppSettings
):
    """The conversation, as clients see it (messages, tool calls, active run)."""
    from app.api.v1.conversations import conversation_detail

    agent, owner = device
    await _device_session(db, agent, conversation_id)
    conversation = await conversation_svc.get_owned_conversation(db, owner.id, conversation_id)
    return await conversation_detail(db, conversation)


@router.post("/device/chats/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def device_chat_message(
    conversation_id: uuid.UUID,
    body: DeviceMessage,
    request: Request,
    device: Device,
    db: DB,
    settings: AppSettings,
):
    """Send a message; the reply runs as any other, its tools on this machine."""
    from app.schemas.conversations import MessageSubmit
    from app.services.chat import submit_message

    agent, owner = device
    session = await _device_session(db, agent, conversation_id)
    if session.status in ENDED_SESSION_STATUSES:
        raise AppError(409, "remote_session_ended", "This chat's session on the machine has ended")
    return await submit_message(
        db,
        owner,
        conversation_id,
        MessageSubmit(client_message_id=body.client_message_id, content=body.content),
        settings,
    )


@router.get("/device/runs/{run_id}/events", response_class=StreamingResponse)
async def device_run_events(
    run_id: uuid.UUID,
    request: Request,
    device: Device,
    db: DB,
    factory: Factory,
    settings: AppSettings,
    after: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """The reply's live events (the same stream the apps follow)."""
    from app.api.v1.runs import _event_stream

    agent, _ = device
    await _device_run(db, agent, run_id)
    return StreamingResponse(
        _event_stream(request, factory, settings, run_id, after or 0),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/device/runs/{run_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def device_run_cancel(
    run_id: uuid.UUID, request: Request, device: Device, db: DB, settings: AppSettings
) -> Response:
    from app.services.runs import request_cancel

    agent, _ = device
    run = await _device_run(db, agent, run_id)
    await request_cancel(db, run)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _device_call(db: AsyncSession, agent: RemoteAgent, call_id: uuid.UUID) -> ToolCall:
    call = await db.get(ToolCall, call_id)
    if call is None:
        raise AppError(404, "not_found", "Tool call not found")
    await _device_run(db, agent, call.run_id)
    return call


@router.post("/device/tool-calls/{call_id}/approve")
async def device_approve(
    call_id: uuid.UUID,
    body: DeviceApproval,
    request: Request,
    device: Device,
    db: DB,
    settings: AppSettings,
):
    """Approve a request in a chat on this machine (from its own terminal)."""
    from app.services.tools.calls import approve, tool_call_out

    agent, owner = device
    await _device_call(db, agent, call_id)
    call = await approve(
        db, owner, call_id, body.args_hash, remember=body.remember, ip_address=_ip(request)
    )
    await db.commit()
    return tool_call_out(call)


@router.post("/device/tool-calls/{call_id}/deny")
async def device_deny(
    call_id: uuid.UUID,
    body: DeviceDenial,
    request: Request,
    device: Device,
    db: DB,
    settings: AppSettings,
):
    from app.services.tools.calls import deny, tool_call_out

    agent, owner = device
    await _device_call(db, agent, call_id)
    call = await deny(db, owner, call_id, body.reason, ip_address=_ip(request))
    await db.commit()
    return tool_call_out(call)


@router.websocket("/agent/connect")
async def agent_connect(websocket: WebSocket, factory: Factory, settings: AppSettings) -> None:
    scheme, _, value = websocket.headers.get("authorization", "").partition(" ")
    claims = identity.verify_token(value.strip(), settings) if scheme.lower() == "bearer" else None
    if claims is None:
        await websocket.close(code=4401, reason="invalid token")
        return
    async with factory() as db:
        agent = await db.get(RemoteAgent, claims.agent_id)
        if agent is None or agent.status != "active":
            await websocket.close(code=4401, reason="machine not authorised")
            return
    await websocket.accept()
    ip = websocket.client.host if websocket.client else None
    connection = AgentConnection(websocket, factory, settings, claims.agent_id, ip)
    try:
        await connection.serve()
    except WebSocketDisconnect:
        pass
