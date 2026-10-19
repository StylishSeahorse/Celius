"""Scheduled tasks, MCP servers (administrators only) and the activity panel."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthContext, CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import (
    Conversation,
    Job,
    JobStatus,
    McpServer,
    Run,
    RunStatus,
    ScheduledTask,
    ToolCall,
    ToolCallStatus,
)
from app.schemas.tools import (
    ActivityItem,
    ActivityList,
    McpServerCreate,
    McpServerOut,
    McpServerUpdate,
    ScheduleCreate,
    ScheduleOut,
    ScheduleUpdate,
)
from app.services import schedules as schedule_svc
from app.services.audit import audit
from app.services.runs import request_cancel
from app.services.secrets import seal
from app.services.tools.mcp import McpClient, McpError, server_headers

router = APIRouter(tags=["automation"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ---------------------------------------------------------------------- schedules


def _schedule_out(task: ScheduledTask) -> ScheduleOut:
    return ScheduleOut(
        id=task.id,
        name=task.name,
        prompt=task.prompt,
        kind=task.kind,
        interval_minutes=task.interval_minutes,
        time_of_day=task.time_of_day,
        weekdays=task.weekdays or [],
        timezone=task.timezone,
        model_override=task.model_override,
        tool_profile=task.tool_profile,
        project_id=task.project_id,
        enabled=task.enabled,
        conversation_id=task.conversation_id,
        next_run_at=task.next_run_at,
        last_run_at=task.last_run_at,
        last_run_id=task.last_run_id,
        last_status=task.last_status,
        last_error=task.last_error,
        created_at=task.created_at,
    )


async def _owned_schedule(db: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID):
    task = await db.scalar(
        select(ScheduledTask).where(ScheduledTask.id == task_id, ScheduledTask.owner_id == user_id)
    )
    if task is None:
        raise not_found("Scheduled task")
    return task


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(auth: CurrentAuth, db: DB) -> list[ScheduleOut]:
    rows = await db.scalars(
        select(ScheduledTask)
        .where(ScheduledTask.owner_id == auth.user.id)
        .order_by(ScheduledTask.created_at)
    )
    return [_schedule_out(t) for t in rows.all()]


@router.post("/schedules", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    body: ScheduleCreate, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> ScheduleOut:
    await schedule_svc.check_quota(db, auth.user, settings)
    task = ScheduledTask(owner_id=auth.user.id, **body.model_dump())
    await schedule_svc.validate(db, auth.user, task, settings)
    task.next_run_at = schedule_svc.next_occurrence(task, utcnow()) if task.enabled else None
    db.add(task)
    await db.flush()
    audit(
        db,
        auth.user.id,
        "schedule.created",
        "schedule",
        task.id,
        _ip(request),
        name=task.name,
        kind=task.kind,
        tool_profile=task.tool_profile,
    )
    await db.commit()
    return _schedule_out(task)


@router.patch("/schedules/{task_id}", response_model=ScheduleOut)
async def update_schedule(
    task_id: uuid.UUID,
    body: ScheduleUpdate,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
) -> ScheduleOut:
    task = await _owned_schedule(db, auth.user.id, task_id)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is not None or key in ("tool_profile", "model_override", "project_id"):
            setattr(task, key, value)
    await schedule_svc.validate(db, auth.user, task, settings)
    task.next_run_at = schedule_svc.next_occurrence(task, utcnow()) if task.enabled else None
    audit(
        db,
        auth.user.id,
        "schedule.updated",
        "schedule",
        task.id,
        _ip(request),
        **{k: v for k, v in changes.items() if k != "prompt"},
    )
    await db.commit()
    return _schedule_out(task)


@router.delete("/schedules/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(task_id: uuid.UUID, request: Request, auth: CurrentAuth, db: DB) -> None:
    task = await _owned_schedule(db, auth.user.id, task_id)
    await db.delete(task)
    audit(db, auth.user.id, "schedule.deleted", "schedule", task_id, _ip(request))
    await db.commit()


@router.post("/schedules/{task_id}/run", response_model=ScheduleOut)
async def run_schedule_now(
    task_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> ScheduleOut:
    task = await _owned_schedule(db, auth.user.id, task_id)
    if await schedule_svc.fire(db, task, settings) is None:
        await db.refresh(task)
        raise AppError(409, "not_started", task.last_error or "The task could not start")
    await db.refresh(task)
    return _schedule_out(task)


# ---------------------------------------------------------------------- MCP servers


def require_admin(auth: CurrentAuth) -> AuthContext:
    if not auth.user.is_admin:
        raise AppError(403, "forbidden", "Only an administrator can manage MCP servers")
    return auth


Admin = Annotated[AuthContext, Depends(require_admin)]


def _server_out(server: McpServer) -> McpServerOut:
    return McpServerOut(
        id=server.id,
        name=server.name,
        url=server.url,
        enabled=server.enabled,
        allow_private_network=server.allow_private_network,
        has_credentials=server.credentials is not None,
        tools=server.tools or [],
        last_error=server.last_error,
        last_checked_at=server.last_checked_at,
        created_at=server.created_at,
    )


async def _owned_server(db: AsyncSession, user_id: uuid.UUID, server_id: uuid.UUID) -> McpServer:
    server = await db.scalar(
        select(McpServer).where(McpServer.id == server_id, McpServer.owner_id == user_id)
    )
    if server is None:
        raise not_found("MCP server")
    return server


async def _discover(server: McpServer, settings: Settings) -> None:
    """Fetch the server's tool list. Failure is recorded, not raised."""
    try:
        async with McpClient(
            server.url,
            server_headers(settings, server),
            timeout=settings.mcp_timeout_seconds,
            allow_private=server.allow_private_network,
        ) as client:
            server.tools = await client.list_tools()
        server.last_error = None
    except McpError as exc:
        server.last_error = str(exc)[:500]
    server.last_checked_at = utcnow()


@router.get("/mcp-servers", response_model=list[McpServerOut])
async def list_servers(auth: Admin, db: DB) -> list[McpServerOut]:
    rows = await db.scalars(
        select(McpServer).where(McpServer.owner_id == auth.user.id).order_by(McpServer.name)
    )
    return [_server_out(s) for s in rows.all()]


@router.post("/mcp-servers", response_model=McpServerOut, status_code=status.HTTP_201_CREATED)
async def create_server(
    body: McpServerCreate, request: Request, auth: Admin, db: DB, settings: AppSettings
) -> McpServerOut:
    server = McpServer(
        owner_id=auth.user.id,
        name=body.name,
        url=body.url,
        enabled=body.enabled,
        allow_private_network=body.allow_private_network,
        credentials=seal(settings, body.headers, "mcp") if body.headers else None,
        tools=[],
    )
    db.add(server)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise AppError(409, "name_taken", "An MCP server with this name exists") from exc
    await _discover(server, settings)
    audit(
        db,
        auth.user.id,
        "mcp_server.created",
        "mcp_server",
        server.id,
        _ip(request),
        name=server.name,
        url=server.url,
        allow_private_network=server.allow_private_network,
    )
    await db.commit()
    return _server_out(server)


@router.patch("/mcp-servers/{server_id}", response_model=McpServerOut)
async def update_server(
    server_id: uuid.UUID,
    body: McpServerUpdate,
    request: Request,
    auth: Admin,
    db: DB,
    settings: AppSettings,
) -> McpServerOut:
    server = await _owned_server(db, auth.user.id, server_id)
    changes = body.model_dump(exclude_unset=True)
    if (
        "url" in changes
        and changes["url"]
        and not changes["url"].startswith(("https://", "http://"))
    ):
        raise AppError(422, "invalid_url", "The URL must start with https:// or http://")
    for key in ("url", "enabled", "allow_private_network"):
        if changes.get(key) is not None:
            setattr(server, key, changes[key])
    if "headers" in changes:
        server.credentials = seal(settings, body.headers, "mcp") if body.headers else None
    if server.enabled:
        await _discover(server, settings)
    audit(
        db,
        auth.user.id,
        "mcp_server.updated",
        "mcp_server",
        server.id,
        _ip(request),
        **{k: v for k, v in changes.items() if k != "headers"},
        headers_changed="headers" in changes,
    )
    await db.commit()
    return _server_out(server)


@router.post("/mcp-servers/{server_id}/refresh", response_model=McpServerOut)
async def refresh_server(
    server_id: uuid.UUID, auth: Admin, db: DB, settings: AppSettings
) -> McpServerOut:
    server = await _owned_server(db, auth.user.id, server_id)
    await _discover(server, settings)
    await db.commit()
    return _server_out(server)


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: uuid.UUID, request: Request, auth: Admin, db: DB) -> None:
    server = await _owned_server(db, auth.user.id, server_id)
    await db.delete(server)
    audit(db, auth.user.id, "mcp_server.deleted", "mcp_server", server_id, _ip(request))
    await db.commit()


# ---------------------------------------------------------------------- activity

ACTIVE_RUNS = (RunStatus.queued, RunStatus.running, RunStatus.awaiting_approval)


@router.get("/activity", response_model=ActivityList)
async def activity(
    auth: CurrentAuth, db: DB, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> ActivityList:
    """Recent and running work: replies (with their tool calls) and background jobs."""
    run_rows = (
        await db.execute(
            select(Run, Conversation.title)
            .join(Conversation, Conversation.id == Run.conversation_id)
            .where(Run.owner_id == auth.user.id, Conversation.deleted_at.is_(None))
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
    ).all()
    run_ids = [run.id for run, _ in run_rows]
    counts: dict[uuid.UUID, tuple[int, int]] = {}
    if run_ids:
        for run_id, total, pending in (
            await db.execute(
                select(
                    ToolCall.run_id,
                    func.count(),
                    func.sum(
                        case((ToolCall.status == ToolCallStatus.pending_approval, 1), else_=0)
                    ),
                )
                .where(ToolCall.run_id.in_(run_ids))
                .group_by(ToolCall.run_id)
            )
        ).all():
            counts[run_id] = (total, pending or 0)
    items = [
        ActivityItem(
            kind="run",
            id=run.id,
            status=run.status.value,
            title=title or "Untitled",
            conversation_id=run.conversation_id,
            source=(run.params or {}).get("source", "chat"),
            tool_calls=counts.get(run.id, (0, 0))[0],
            pending_approvals=counts.get(run.id, (0, 0))[1],
            error=run.error_message,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            cancellable=run.status in ACTIVE_RUNS,
        )
        for run, title in run_rows
    ]
    jobs = (
        await db.scalars(
            select(Job)
            .where(Job.owner_id == auth.user.id)
            .order_by(Job.created_at.desc())
            .limit(limit)
        )
    ).all()
    items += [
        ActivityItem(
            kind="job",
            id=job.id,
            status=job.status.value,
            title=job.kind,
            source=job.kind,
            error=job.error,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            cancellable=job.status == JobStatus.queued,
        )
        for job in jobs
    ]
    items.sort(key=lambda i: i.created_at, reverse=True)
    return ActivityList(items=items[:limit])


@router.post("/activity/runs/{run_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_activity_run(run_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    run = await db.scalar(select(Run).where(Run.id == run_id, Run.owner_id == auth.user.id))
    if run is None:
        raise not_found("Run")
    await request_cancel(db, run)
    await db.commit()


@router.post("/activity/jobs/{job_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(job_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    """Queued jobs only; a running job is short and finishes (or fails) on its own."""
    job = await db.scalar(select(Job).where(Job.id == job_id, Job.owner_id == auth.user.id))
    if job is None:
        raise not_found("Job")
    if job.status != JobStatus.queued:
        raise AppError(409, "not_cancellable", "Only queued jobs can be cancelled")
    job.status = JobStatus.failed
    job.error = "Cancelled"
    job.finished_at = utcnow()
    await db.commit()
