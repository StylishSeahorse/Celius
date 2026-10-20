"""Scheduled tasks: a stored prompt that starts a run in its own conversation.

The worker fires due tasks. Claiming is one conditional UPDATE on `next_run_at`, so a
task fires once per occurrence even with several workers. A missed occurrence (worker
down) fires once on recovery, not once per missed slot.
"""

import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.errors import AppError
from app.models import Conversation, Run, ScheduledTask, User
from app.schemas.conversations import ConversationCreate, MessageSubmit
from app.services.access import require_project
from app.services.chat import submit_message
from app.services.conversations import create_conversation
from app.services.tools.policy import validate_profile_key

log = logging.getLogger(__name__)

FIRE_BATCH = 10


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AppError(422, "invalid_timezone", f"Unknown time zone {name!r}") from exc


def next_occurrence(task: ScheduledTask, after: datetime) -> datetime:
    """The first time strictly after `after` (UTC) that the task should fire."""
    if task.kind == "interval":
        return after + timedelta(minutes=task.interval_minutes or 60)
    zone = _zone(task.timezone)
    hour, minute = (int(x) for x in (task.time_of_day or "09:00").split(":"))
    local = after.astimezone(zone)
    days = set(task.weekdays) if task.kind == "weekly" else set(range(7))
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        if candidate > local:
            return candidate.astimezone(after.tzinfo)
    raise AppError(422, "invalid_schedule", "The schedule never fires")


async def validate(db: AsyncSession, user: User, task: ScheduledTask, settings: Settings) -> None:
    if task.kind == "interval":
        if (
            not task.interval_minutes
            or task.interval_minutes < settings.schedule_min_interval_minutes
        ):
            raise AppError(
                422,
                "interval_too_short",
                f"Run at most every {settings.schedule_min_interval_minutes} minutes",
            )
    else:
        if not task.time_of_day:
            raise AppError(422, "invalid_schedule", "A daily or weekly task needs a time of day")
        if task.kind == "weekly" and (
            not task.weekdays or any(d not in range(7) for d in task.weekdays)
        ):
            raise AppError(422, "invalid_schedule", "Pick weekdays (0 = Monday … 6 = Sunday)")
    _zone(task.timezone)
    await validate_profile_key(db, user.id, task.tool_profile)
    if task.project_id:
        await require_project(db, user.id, task.project_id)


async def check_quota(db: AsyncSession, user: User, settings: Settings) -> None:
    count = await db.scalar(
        select(func.count()).select_from(ScheduledTask).where(ScheduledTask.owner_id == user.id)
    )
    if count >= settings.schedule_max_per_user:
        raise AppError(
            409, "quota_exceeded", f"At most {settings.schedule_max_per_user} scheduled tasks"
        )


async def _claim(db: AsyncSession, task: ScheduledTask, now: datetime) -> bool:
    following = next_occurrence(task, now)
    claimed = await db.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.id == task.id,
            ScheduledTask.enabled.is_(True),
            ScheduledTask.next_run_at == task.next_run_at,
        )
        .values(next_run_at=following, last_run_at=now)
        .returning(ScheduledTask.id)
    )
    return claimed.scalar() is not None


async def fire(db: AsyncSession, task: ScheduledTask, settings: Settings) -> uuid.UUID | None:
    """Start one run of `task` now. Returns the run id, or None if it was skipped."""
    user = await db.get(User, task.owner_id)
    if user is None or not user.is_active:
        return None
    conversation = (
        await db.get(Conversation, task.conversation_id) if task.conversation_id else None
    )
    if conversation is None or conversation.deleted_at is not None:
        conversation, _ = await create_conversation(
            db,
            user.id,
            ConversationCreate(
                title=f"⏱ {task.name}"[:300],
                project_id=task.project_id,
                model_override=task.model_override,
                tool_profile=task.tool_profile,
            ),
        )
        task.conversation_id = conversation.id
        await db.commit()
    elif conversation.tool_profile != task.tool_profile:
        conversation.tool_profile = task.tool_profile
    stamp = utcnow().strftime("%Y%m%dT%H%M%S")
    try:
        result = await submit_message(
            db,
            user,
            conversation.id,
            MessageSubmit(
                client_message_id=f"schedule-{task.id.hex[:16]}-{stamp}",
                content=task.prompt,
                model=task.model_override,
            ),
            settings,
        )
    except AppError as exc:
        await db.rollback()
        await db.execute(
            update(ScheduledTask)
            .where(ScheduledTask.id == task.id)
            .values(last_status="skipped", last_error=exc.message[:500])
        )
        await db.commit()
        return None
    run = await db.get(Run, result.run.id)
    if run is not None:
        run.params = {**(run.params or {}), "source": "schedule", "schedule_id": str(task.id)}
    await db.execute(
        update(ScheduledTask)
        .where(ScheduledTask.id == task.id)
        .values(last_status="started", last_error=None, last_run_id=result.run.id)
    )
    await db.commit()
    return result.run.id


async def fire_due(factory: SessionFactory, settings: Settings) -> int:
    fired = 0
    now = utcnow()
    async with factory() as db:
        due = (
            await db.scalars(
                select(ScheduledTask)
                .where(ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at <= now)
                .order_by(ScheduledTask.next_run_at)
                .limit(FIRE_BATCH)
            )
        ).all()
    for task in due:
        async with factory() as db:
            task = await db.get(ScheduledTask, task.id)
            if task is None or not await _claim(db, task, now):
                await db.rollback()
                continue  # another worker took it, or it changed
            await db.commit()
            try:
                if await fire(db, task, settings):
                    fired += 1
            except Exception:
                log.exception("Scheduled task %s failed to start", task.id)
                await db.rollback()
    return fired
