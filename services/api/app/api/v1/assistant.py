"""Personal assistant: the user's tasks, the daily briefing and assistant settings
(ADR C-10). The model reaches the same data through the task tools."""

import uuid
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import PRIORITIES, ScheduledTask, UserSettings, UserTask
from app.schemas.assistant import (
    AgentTaskBrief,
    AssistantSettings,
    AssistantSettingsUpdate,
    Briefing,
    UserTaskChanged,
    UserTaskCreate,
    UserTaskOut,
    UserTaskUpdate,
)
from app.services import schedules as schedule_svc
from app.services.assistant import tasks
from app.services.audit import audit
from app.services.sync import record_sync

router = APIRouter(tags=["assistant"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
PRIORITY_NAMES = {v: k for k, v in PRIORITIES.items()}
BRIEFING_PROMPT = (
    "Prepare my daily briefing: call daily_briefing, then give me a realistic plan for "
    "today in a few short lines (what first, what can wait). Don't change any task."
)


def _out(task: UserTask, tz: ZoneInfo, by_id: dict | None = None) -> UserTaskOut:
    day = tasks.local_day(task.due_at, tz)
    return UserTaskOut(
        id=task.id,
        title=task.title,
        notes=task.notes,
        status=task.status,
        priority=PRIORITY_NAMES.get(task.priority, "normal"),
        due_at=task.due_at,
        due_all_day=task.due_all_day,
        due_date=day.isoformat() if day else None,
        overdue=tasks.is_overdue(task, tz),
        remind_at=task.remind_at,
        reminded_at=task.reminded_at,
        recurrence=task.recurrence,
        project_id=task.project_id,
        parent_id=task.parent_id,
        blocked_by=task.blocked_by or [],
        blocked=tasks.blocked(task, by_id or {}),
        tags=task.tags or [],
        attachment_ids=task.attachment_ids or [],
        source=task.source,
        source_conversation_id=task.source_conversation_id,
        completed_at=task.completed_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def _tz(db: AsyncSession, owner_id: uuid.UUID, settings: Settings) -> ZoneInfo:
    return tasks.zone((await tasks.user_prefs(db, owner_id, settings)).timezone)


async def _owned(db: AsyncSession, owner_id: uuid.UUID, task_id: uuid.UUID) -> UserTask:
    task = await tasks._owned_task(db, owner_id, task_id)
    if task is None:
        raise not_found("Task")
    return task


async def _blockers(db: AsyncSession, rows: list[UserTask]) -> dict[str, UserTask]:
    ids = {b for t in rows for b in (t.blocked_by or [])}
    if not ids:
        return {}
    found = (
        await db.scalars(select(UserTask).where(UserTask.id.in_([uuid.UUID(i) for i in ids])))
    ).all()
    return {str(t.id): t for t in found}


# ---------------------------------------------------------------------- tasks


@router.get("/tasks", response_model=list[UserTaskOut])
async def list_tasks(
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    status_: Annotated[
        str, Query(alias="status", pattern="^(active|done|cancelled|suggested|all)$")
    ] = "active",
    project_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[UserTaskOut]:
    statuses = {
        "active": ("open", "in_progress"),
        "done": ("done",),
        "cancelled": ("cancelled",),
        "suggested": ("suggested",),
        "all": ("suggested", "open", "in_progress", "done", "cancelled"),
    }[status_]
    rows = await tasks.list_tasks(db, auth.user.id, statuses=statuses, project_id=project_id, q=q)
    tz = await _tz(db, auth.user.id, settings)
    by_id = await _blockers(db, rows)
    return [_out(t, tz, by_id) for t in rows]


@router.post("/tasks", response_model=UserTaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: UserTaskCreate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> UserTaskOut:
    task = await tasks.create(
        db,
        auth.user.id,
        title=body.title,
        settings=settings,
        notes=body.notes,
        priority=PRIORITIES[body.priority],
        due=body.due,
        remind=body.remind,
        recurrence=body.recurrence.model_dump() if body.recurrence else None,
        project_id=body.project_id,
        parent_id=body.parent_id,
        blocked_by=[str(b) for b in body.blocked_by],
        tags=body.tags,
        attachment_ids=[str(a) for a in body.attachment_ids],
    )
    await db.commit()
    return _out(task, await _tz(db, auth.user.id, settings), await _blockers(db, [task]))


@router.get("/tasks/{user_task_id}", response_model=UserTaskOut)
async def get_task(
    user_task_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> UserTaskOut:
    task = await _owned(db, auth.user.id, user_task_id)
    return _out(task, await _tz(db, auth.user.id, settings), await _blockers(db, [task]))


@router.patch("/tasks/{user_task_id}", response_model=UserTaskChanged)
async def update_task(
    user_task_id: uuid.UUID, body: UserTaskUpdate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> UserTaskChanged:
    task = await _owned(db, auth.user.id, user_task_id)
    changes = body.model_dump(exclude_unset=True, exclude={"clear_recurrence"})
    if "priority" in changes:
        changes["priority"] = PRIORITIES[changes["priority"]]
    for key in ("due", "remind"):
        if changes.get(key) == "":
            changes[key] = None
    if body.clear_recurrence:
        changes["recurrence"] = None
    elif body.recurrence is not None:
        changes["recurrence"] = body.recurrence.model_dump()
    for key in ("blocked_by", "attachment_ids"):
        if changes.get(key) is not None:
            changes[key] = [str(v) for v in changes[key]]
    following = await tasks.change(db, task, changes, settings, actor="user")
    await db.commit()
    tz = await _tz(db, auth.user.id, settings)
    return UserTaskChanged(
        task=_out(task, tz, await _blockers(db, [task])),
        next_occurrence=_out(following, tz) if following else None,
    )


@router.post("/tasks/{user_task_id}/accept", response_model=UserTaskOut)
async def accept_task(
    user_task_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> UserTaskOut:
    """Accept a task Celius suggested from a chat."""
    task = await _owned(db, auth.user.id, user_task_id)
    await tasks.accept_suggestion(db, task)
    await db.commit()
    return _out(task, await _tz(db, auth.user.id, settings))


@router.delete("/tasks/{user_task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(user_task_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    """Delete a task (and its subtasks). Dismissing a suggestion is deleting it."""
    task = await _owned(db, auth.user.id, user_task_id)
    await db.execute(delete(UserTask).where(UserTask.id == task.id))
    audit(db, auth.user.id, "user_task.deleted", "user_task", task.id)
    record_sync(db, auth.user.id, "user_task", task.id, operation="delete")
    await db.commit()


# ---------------------------------------------------------------------- briefing


@router.get("/assistant/briefing", response_model=Briefing)
async def get_briefing(auth: CurrentAuth, db: DB, settings: AppSettings) -> Briefing:
    """Today: overdue, due, upcoming, in progress, a suggested order, suggestions to
    review, Celius's open work and what waits for you. Computed, no model involved."""
    data = await tasks.briefing(db, auth.user.id, settings)
    tz = tasks.zone(data["timezone"])
    every = [
        t
        for key in ("overdue", "due_today", "upcoming", "in_progress", "plan", "blocked")
        for t in data[key]
    ]
    by_id = await _blockers(db, every)

    def outs(key: str) -> list[UserTaskOut]:
        return [_out(t, tz, by_id) for t in data[key]]

    return Briefing(
        date=data["date"],
        timezone=data["timezone"],
        overdue=outs("overdue"),
        due_today=outs("due_today"),
        upcoming=outs("upcoming"),
        in_progress=outs("in_progress"),
        plan=outs("plan"),
        suggestions=outs("suggestions"),
        blocked=outs("blocked"),
        agent_tasks=[
            AgentTaskBrief(
                id=t.id,
                conversation_id=t.conversation_id,
                conversation_title=title,
                objective=t.objective,
                status=t.status,
            )
            for t, title in data["agent_tasks"]
        ],
        waiting=data["waiting"],
        calendar=data["calendar"],
        text=tasks.briefing_text(data),
    )


# ---------------------------------------------------------------------- settings


@router.get("/assistant/settings", response_model=AssistantSettings)
async def get_assistant_settings(
    auth: CurrentAuth, db: DB, settings: AppSettings
) -> AssistantSettings:
    prefs = await tasks.user_prefs(db, auth.user.id, settings)
    return AssistantSettings(
        timezone=prefs.timezone,
        suggest_tasks=prefs.suggest_tasks,
        briefing_time=prefs.briefing_time,
    )


@router.put("/assistant/settings", response_model=AssistantSettings)
async def update_assistant_settings(
    body: AssistantSettingsUpdate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> AssistantSettings:
    row = await db.get(UserSettings, auth.user.id)
    if row is None:
        row = UserSettings(user_id=auth.user.id, preferences={})
        db.add(row)
    current = dict(((row.preferences or {}).get("assistant")) or {})
    if body.timezone is not None:
        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise AppError(422, "invalid_timezone", f"Unknown time zone {body.timezone!r}") from exc
        current["timezone"] = body.timezone
    if body.suggest_tasks is not None:
        current["suggest_tasks"] = body.suggest_tasks
    if body.briefing_time is not None:
        current = await _briefing_schedule(db, auth.user, current, body.briefing_time, settings)
    row.preferences = {**(row.preferences or {}), "assistant": current}
    audit(db, auth.user.id, "assistant.settings_changed", "user", auth.user.id)
    await db.commit()
    return await get_assistant_settings(auth, db, settings)


async def _briefing_schedule(db, user, current: dict, time_of_day: str, settings) -> dict:
    """A daily briefing is an ordinary scheduled task (Tools > Schedules shows it), with
    the read-only tool profile: it can read tasks but never change them."""
    existing = None
    if current.get("briefing_schedule_id"):
        existing = await db.scalar(
            select(ScheduledTask).where(
                ScheduledTask.id == uuid.UUID(current["briefing_schedule_id"]),
                ScheduledTask.owner_id == user.id,
            )
        )
    if not time_of_day:
        if existing is not None:
            await db.delete(existing)
        current.pop("briefing_schedule_id", None)
        current["briefing_time"] = None
        return current
    timezone = current.get("timezone") or settings.default_timezone
    if existing is None:
        await schedule_svc.check_quota(db, user, settings)
        existing = ScheduledTask(
            owner_id=user.id,
            name="Daily briefing",
            prompt=BRIEFING_PROMPT,
            kind="daily",
            weekdays=[],
            tool_profile="read_only",
            enabled=True,
        )
        db.add(existing)
    existing.time_of_day = time_of_day
    existing.timezone = timezone
    await schedule_svc.validate(db, user, existing, settings)
    existing.next_run_at = schedule_svc.next_occurrence(existing, utcnow())
    await db.flush()
    current["briefing_schedule_id"] = str(existing.id)
    current["briefing_time"] = time_of_day
    return current
