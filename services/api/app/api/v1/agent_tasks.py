"""Agent tasks: the structured state of multi-step work, for the activity view (ADR C-08).

The model keeps a task current with the plan tools; the user can see it, stop it
(abandon), mark it done, or resume an interrupted one in a new reply.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import OPEN_TASK_STATUSES, AgentTask, AgentTaskEvent, Conversation
from app.schemas.agent import (
    AgentTaskDetail,
    AgentTaskEventOut,
    AgentTaskOut,
    AgentTaskResumed,
    AgentTaskUpdate,
    PlanStep,
)
from app.schemas.conversations import MessageSubmit
from app.services.agent import tasks
from app.services.audit import audit
from app.services.chat import submit_message
from app.services.sync import record_sync

router = APIRouter(tags=["agent tasks"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _out(task: AgentTask, title: str | None = None) -> AgentTaskOut:
    return AgentTaskOut(
        id=task.id,
        conversation_id=task.conversation_id,
        conversation_title=title,
        objective=task.objective,
        status=task.status,
        status_reason=task.status_reason,
        plan=[PlanStep(**s) for s in task.plan],
        blockers=task.blockers,
        files=task.files,
        verifications=task.verifications,
        notes=task.notes,
        verified=tasks.verified(task),
        runs=task.runs,
        tool_calls=task.tool_calls,
        failed_tool_calls=task.failed_tool_calls,
        last_run_id=task.last_run_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        finished_at=task.finished_at,
    )


async def _owned(db: AsyncSession, owner_id: uuid.UUID, task_id: uuid.UUID) -> AgentTask:
    task = await db.scalar(
        select(AgentTask).where(AgentTask.id == task_id, AgentTask.owner_id == owner_id)
    )
    if task is None:
        raise not_found("Task")
    return task


async def _detail(db: AsyncSession, task: AgentTask) -> AgentTaskDetail:
    events = (
        await db.scalars(
            select(AgentTaskEvent)
            .where(AgentTaskEvent.task_id == task.id)
            .order_by(AgentTaskEvent.created_at.desc())
            .limit(200)
        )
    ).all()
    title = await db.scalar(
        select(Conversation.title).where(Conversation.id == task.conversation_id)
    )
    return AgentTaskDetail(
        **_out(task, title).model_dump(),
        events=[
            AgentTaskEventOut(
                id=e.id,
                run_id=e.run_id,
                kind=e.kind,
                summary=e.summary,
                detail=e.detail or {},
                created_at=e.created_at,
            )
            for e in events
        ],
    )


@router.get("/agent-tasks", response_model=list[AgentTaskOut])
async def list_tasks(
    auth: CurrentAuth,
    db: DB,
    status_: Annotated[
        str,
        Query(
            alias="status", pattern="^(open|all|active|paused|blocked|completed|failed|abandoned)$"
        ),
    ] = "open",
    conversation_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AgentTaskOut]:
    query = (
        select(AgentTask, Conversation.title)
        .join(Conversation, Conversation.id == AgentTask.conversation_id)
        .where(AgentTask.owner_id == auth.user.id, Conversation.deleted_at.is_(None))
    )
    if status_ == "open":
        query = query.where(AgentTask.status.in_(OPEN_TASK_STATUSES))
    elif status_ != "all":
        query = query.where(AgentTask.status == status_)
    if conversation_id:
        query = query.where(AgentTask.conversation_id == conversation_id)
    rows = (await db.execute(query.order_by(AgentTask.updated_at.desc()).limit(limit))).all()
    return [_out(task, title) for task, title in rows]


@router.get("/agent-tasks/{agent_task_id}", response_model=AgentTaskDetail)
async def get_task(agent_task_id: uuid.UUID, auth: CurrentAuth, db: DB) -> AgentTaskDetail:
    return await _detail(db, await _owned(db, auth.user.id, agent_task_id))


@router.patch("/agent-tasks/{agent_task_id}", response_model=AgentTaskDetail)
async def update_task(
    agent_task_id: uuid.UUID, body: AgentTaskUpdate, auth: CurrentAuth, db: DB
) -> AgentTaskDetail:
    task = await _owned(db, auth.user.id, agent_task_id)
    if body.status in ("active", "paused") and task.status not in OPEN_TASK_STATUSES:
        other = await tasks.open_task(db, task.conversation_id)
        if other is not None and other.id != task.id:
            raise AppError(409, "task_open", "This chat already has an open task")
    if body.objective is not None:
        task.objective = body.objective.strip()
        tasks.event(db, task, "edited", "Objective edited by you")
    if body.status is not None and body.status != task.status:
        reason = {"abandoned": "Stopped by you", "paused": "Paused by you"}.get(body.status)
        tasks.set_status(db, task, body.status, reason, by="user")
    audit(db, auth.user.id, "agent_task.updated", "agent_task", task.id, status=task.status)
    task.updated_at = utcnow()
    await db.commit()
    return await _detail(db, task)


@router.post("/agent-tasks/{agent_task_id}/resume", response_model=AgentTaskResumed)
async def resume_task(
    agent_task_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> AgentTaskResumed:
    """Continue an interrupted task in a new reply. The reply is given the stored state,
    so it doesn't depend on the chat's history still fitting in the context."""
    task = await _owned(db, auth.user.id, agent_task_id)
    if task.status not in OPEN_TASK_STATUSES:
        raise AppError(409, "task_closed", f"This task is {task.status}")
    if task.status == "blocked":
        tasks.set_status(db, task, "active", "Resumed by you", by="user")
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
    result = await submit_message(
        db,
        auth.user,
        task.conversation_id,
        MessageSubmit(
            client_message_id=f"resume-{task.id.hex[:12]}-{stamp}",
            content=f"Continue the task: {task.objective}",
        ),
        settings,
    )
    return AgentTaskResumed(conversation_id=task.conversation_id, run_id=result.run.id)


@router.delete("/agent-tasks/{agent_task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(agent_task_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    task = await _owned(db, auth.user.id, agent_task_id)
    await db.execute(delete(AgentTask).where(AgentTask.id == task.id))
    audit(db, auth.user.id, "agent_task.deleted", "agent_task", task.id)
    record_sync(
        db,
        auth.user.id,
        "agent_task",
        task.id,
        operation="delete",
        payload={"conversation_id": str(task.conversation_id)},
    )
    await db.commit()
