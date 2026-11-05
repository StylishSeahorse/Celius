"""Personal tasks, reminders and the daily briefing (ADR C-10).

Rules the backend keeps, whoever asks (the user in the UI, or the model with a tool):
- a task belongs to one person; its project, parent, blockers and attachments must be
  theirs (or, for a project, one they can read);
- completing a recurring task creates the next occurrence (deadline and reminder moved
  by the same step), so a repeat is never lost;
- reminders fire from the worker on time, whether or not any model is running, and only
  for active tasks (a suggestion is never reminded);
- suggested tasks (noticed in a chat) wait for the user: they are neither listed as
  to-dos nor reminded until accepted.
"""

import calendar
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.errors import AppError
from app.models import (
    ACTIVE_TASK_STATUSES,
    OPEN_TASK_STATUSES,
    AgentTask,
    Conversation,
    StoredFile,
    UserSettings,
    UserTask,
)
from app.services import push
from app.services.access import require_project
from app.services.audit import audit
from app.services.memory.sensitive import redact
from app.services.sync import record_sync

RECURRENCE = ("daily", "weekdays", "weekly", "monthly", "yearly")
MAX_LIST = 200
REMINDER_BATCH = 50


# ---------------------------------------------------------------------- preferences


@dataclass
class AssistantPrefs:
    timezone: str
    suggest_tasks: bool
    briefing_time: str | None  # "HH:MM": a daily briefing schedule exists


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def prefs_from(settings: Settings, row: UserSettings | None) -> AssistantPrefs:
    saved = ((row.preferences if row else None) or {}).get("assistant") or {}
    return AssistantPrefs(
        timezone=saved.get("timezone") or settings.default_timezone,
        suggest_tasks=saved.get("suggest_tasks", settings.assistant_suggest_tasks),
        briefing_time=saved.get("briefing_time"),
    )


async def user_prefs(db: AsyncSession, owner_id: uuid.UUID, settings: Settings) -> AssistantPrefs:
    return prefs_from(settings, await db.get(UserSettings, owner_id))


# ---------------------------------------------------------------------- dates


def parse_due(value: str | None, tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """ "2026-11-06" (all day) or "2026-11-06T15:00" (local time unless it has an offset).
    Returns (UTC datetime, all day)."""
    if value is None or not str(value).strip():
        return None, True
    text = str(value).strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return datetime.combine(day, time(0, 0), tz).astimezone(UTC), True
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(422, "invalid_date", f"Not a date: {text!r} (use YYYY-MM-DD)") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(UTC), False


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)  # SQLite returns naive datetimes
    return value


def local_day(value: datetime | None, tz: ZoneInfo) -> date | None:
    value = _aware(value)
    return value.astimezone(tz).date() if value else None


def is_overdue(task: UserTask, tz: ZoneInfo, now: datetime | None = None) -> bool:
    if task.status not in ACTIVE_TASK_STATUSES or task.due_at is None:
        return False
    now = now or utcnow()
    if task.due_all_day:
        return local_day(task.due_at, tz) < now.astimezone(tz).date()
    return _aware(task.due_at) < now


def _add_months(value: datetime, months: int) -> datetime:
    month = value.month - 1 + months
    year, month = value.year + month // 12, month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def next_due(value: datetime, rule: dict, tz: ZoneInfo) -> datetime:
    """The next occurrence after `value` (computed in local time, so 09:00 stays 09:00
    across daylight-saving changes)."""
    local = _aware(value).astimezone(tz)
    step = max(int(rule.get("interval") or 1), 1)
    freq = rule.get("freq")
    if freq == "daily":
        local = local + timedelta(days=step)
    elif freq == "weekdays":
        local = local + timedelta(days=1)
        while local.weekday() >= 5:
            local += timedelta(days=1)
    elif freq == "weekly":
        local = local + timedelta(weeks=step)
    elif freq == "monthly":
        local = _add_months(local, step)
    elif freq == "yearly":
        local = _add_months(local, 12 * step)
    else:
        raise AppError(422, "invalid_recurrence", f"Unknown repeat {freq!r}")
    naive = local.replace(tzinfo=None)
    return naive.replace(tzinfo=tz).astimezone(UTC)


def check_recurrence(rule: dict | None) -> dict | None:
    if not rule:
        return None
    freq = rule.get("freq")
    if freq not in RECURRENCE:
        raise AppError(422, "invalid_recurrence", f"Repeat must be one of {', '.join(RECURRENCE)}")
    interval = rule.get("interval") or 1
    if not isinstance(interval, int) or not 1 <= interval <= 365:
        raise AppError(422, "invalid_recurrence", "The repeat interval must be 1 to 365")
    return {"freq": freq, "interval": interval}


# ---------------------------------------------------------------------- create / change


async def _owned_task(db: AsyncSession, owner_id: uuid.UUID, task_id) -> UserTask | None:
    try:
        task_id = task_id if isinstance(task_id, uuid.UUID) else uuid.UUID(str(task_id))
    except ValueError:
        return None
    return await db.scalar(
        select(UserTask).where(UserTask.id == task_id, UserTask.owner_id == owner_id)
    )


async def _check_links(db: AsyncSession, task: UserTask) -> None:
    if task.project_id is not None:
        await require_project(db, task.owner_id, task.project_id)
    if task.parent_id is not None:
        parent = await _owned_task(db, task.owner_id, task.parent_id)
        if parent is None or parent.id == task.id:
            raise AppError(422, "invalid_parent", "The parent task doesn't exist")
        seen = {task.id}
        while parent is not None and parent.parent_id is not None:  # no cycles
            if parent.parent_id in seen:
                raise AppError(422, "invalid_parent", "That would make a loop of subtasks")
            seen.add(parent.id)
            parent = await _owned_task(db, task.owner_id, parent.parent_id)
    blockers = []
    for raw in task.blocked_by or []:
        blocker = await _owned_task(db, task.owner_id, raw)
        if blocker is None or blocker.id == task.id:
            raise AppError(422, "invalid_dependency", "A task it depends on doesn't exist")
        blockers.append(str(blocker.id))
    task.blocked_by = list(dict.fromkeys(blockers))
    files = []
    for raw in task.attachment_ids or []:
        try:
            file_id = uuid.UUID(str(raw))
        except ValueError as exc:
            raise AppError(422, "invalid_attachment", "Unknown file") from exc
        found = await db.scalar(
            select(StoredFile.id).where(
                StoredFile.id == file_id, StoredFile.owner_id == task.owner_id
            )
        )
        if found is None:
            raise AppError(422, "invalid_attachment", "Unknown file")
        files.append(str(file_id))
    task.attachment_ids = files


def _changed(db: AsyncSession, task: UserTask) -> None:
    record_sync(db, task.owner_id, "user_task", task.id)


async def create(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    title: str,
    settings: Settings,
    notes: str | None = None,
    status: str = "open",
    priority: int = 1,
    due: str | None = None,
    remind: str | None = None,
    recurrence: dict | None = None,
    project_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    blocked_by: list | None = None,
    tags: list[str] | None = None,
    attachment_ids: list | None = None,
    source: str = "user",
    conversation_id: uuid.UUID | None = None,
) -> UserTask:
    prefs = await user_prefs(db, owner_id, settings)
    tz = zone(prefs.timezone)
    due_at, all_day = parse_due(due, tz)
    remind_at, _ = parse_due(remind, tz)
    title = " ".join(redact(title).split())[:300]
    if len(title) < 1:
        raise AppError(422, "invalid_title", "A task needs a title")
    task = UserTask(
        id=uuid.uuid4(),
        owner_id=owner_id,
        project_id=project_id,
        parent_id=parent_id,
        title=title,
        notes=redact(notes)[:10_000] if notes else None,
        status=status,
        priority=min(max(int(priority), 0), 3),
        due_at=due_at,
        due_all_day=all_day,
        remind_at=remind_at,
        recurrence=check_recurrence(recurrence),
        blocked_by=list(blocked_by or []),
        tags=[str(t)[:40] for t in (tags or [])][:20],
        attachment_ids=list(attachment_ids or []),
        source=source,
        source_conversation_id=conversation_id,
    )
    if task.recurrence and task.due_at is None:
        raise AppError(422, "invalid_recurrence", "A repeating task needs a due date")
    await _check_links(db, task)
    db.add(task)
    await db.flush()
    audit(db, owner_id, "user_task.created", "user_task", task.id, source=source)
    _changed(db, task)
    return task


UPDATABLE = {
    "title",
    "notes",
    "status",
    "priority",
    "due",
    "remind",
    "recurrence",
    "project_id",
    "parent_id",
    "blocked_by",
    "tags",
    "attachment_ids",
}


async def change(
    db: AsyncSession,
    task: UserTask,
    changes: dict,
    settings: Settings,
    *,
    actor: str,
) -> UserTask | None:
    """Apply `changes` (keys from UPDATABLE). Returns the next occurrence when completing
    a repeating task created one."""
    unknown = set(changes) - UPDATABLE
    if unknown:
        raise AppError(422, "invalid_fields", f"Can't change {', '.join(sorted(unknown))}")
    prefs = await user_prefs(db, task.owner_id, settings)
    tz = zone(prefs.timezone)
    previous_status = task.status
    if "title" in changes:
        title = " ".join(redact(changes["title"] or "").split())[:300]
        if not title:
            raise AppError(422, "invalid_title", "A task needs a title")
        task.title = title
    if "notes" in changes:
        task.notes = redact(changes["notes"])[:10_000] if changes["notes"] else None
    if "priority" in changes:
        task.priority = min(max(int(changes["priority"]), 0), 3)
    if "due" in changes:
        task.due_at, task.due_all_day = parse_due(changes["due"], tz)
    if "remind" in changes:
        task.remind_at, _ = parse_due(changes["remind"], tz)
        task.reminded_at = None
    if "recurrence" in changes:
        task.recurrence = check_recurrence(changes["recurrence"])
    for key in ("project_id", "parent_id", "blocked_by", "tags", "attachment_ids"):
        if key in changes:
            setattr(
                task,
                key,
                changes[key]
                if changes[key] is not None
                else ([] if key in ("blocked_by", "tags", "attachment_ids") else None),
            )
    if task.recurrence and task.due_at is None:
        raise AppError(422, "invalid_recurrence", "A repeating task needs a due date")
    await _check_links(db, task)

    following = None
    if "status" in changes and changes["status"] != previous_status:
        status = changes["status"]
        task.status = status
        if status == "done":
            task.completed_at = utcnow()
            if task.recurrence and task.due_at is not None:
                following = await _next_occurrence(db, task, tz)
        elif status in ACTIVE_TASK_STATUSES:
            task.completed_at = None
    audit(
        db,
        task.owner_id,
        "user_task.updated",
        "user_task",
        task.id,
        by=actor,
        fields=sorted(changes),
    )
    _changed(db, task)
    return following


async def _next_occurrence(db: AsyncSession, task: UserTask, tz: ZoneInfo) -> UserTask:
    due = next_due(task.due_at, task.recurrence, tz)
    remind = None
    if task.remind_at is not None:
        remind = _aware(task.remind_at) + (due - _aware(task.due_at))
    following = UserTask(
        id=uuid.uuid4(),
        owner_id=task.owner_id,
        project_id=task.project_id,
        parent_id=task.parent_id,
        title=task.title,
        notes=task.notes,
        status="open",
        priority=task.priority,
        due_at=due,
        due_all_day=task.due_all_day,
        remind_at=remind,
        recurrence=task.recurrence,
        blocked_by=[],
        tags=list(task.tags or []),
        attachment_ids=list(task.attachment_ids or []),
        source=task.source if task.source != "suggested" else "user",
    )
    db.add(following)
    await db.flush()
    _changed(db, following)
    return following


def _same(title: str) -> str:
    return " ".join(title.lower().split())


async def suggest(
    db: AsyncSession, conversation: Conversation, title: str, due: str | None, settings: Settings
) -> UserTask | None:
    """A to-do noticed in a chat, waiting for the user to accept it. Skipped when the
    same title is already suggested or on the list."""
    existing = (
        await db.scalars(
            select(UserTask.title).where(
                UserTask.owner_id == conversation.owner_id,
                UserTask.status.in_(("suggested", "open", "in_progress")),
            )
        )
    ).all()
    if _same(title) in {_same(t) for t in existing}:
        return None
    try:
        return await create(
            db,
            conversation.owner_id,
            title=title,
            settings=settings,
            status="suggested",
            due=due,
            project_id=conversation.project_id,
            source="suggested",
            conversation_id=conversation.id,
        )
    except AppError:
        return None  # e.g. a project the user can no longer read


async def accept_suggestion(db: AsyncSession, task: UserTask) -> None:
    if task.status != "suggested":
        raise AppError(409, "not_suggested", "This task isn't a suggestion")
    task.status = "open"
    audit(db, task.owner_id, "user_task.accepted", "user_task", task.id)
    _changed(db, task)


# ---------------------------------------------------------------------- reading


def blocked(task: UserTask, by_id: dict[str, UserTask]) -> bool:
    return any(
        (other := by_id.get(b)) is not None and other.status in ACTIVE_TASK_STATUSES
        for b in task.blocked_by or []
    )


async def list_tasks(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    statuses: tuple[str, ...] = ACTIVE_TASK_STATUSES,
    project_id: uuid.UUID | None = None,
    q: str | None = None,
    due_before: datetime | None = None,
    min_priority: int | None = None,
    limit: int = MAX_LIST,
) -> list[UserTask]:
    query = select(UserTask).where(UserTask.owner_id == owner_id, UserTask.status.in_(statuses))
    if project_id:
        query = query.where(UserTask.project_id == project_id)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(or_(UserTask.title.ilike(like), UserTask.notes.ilike(like)))
    if due_before is not None:
        query = query.where(UserTask.due_at.is_not(None), UserTask.due_at < due_before)
    if min_priority is not None:
        query = query.where(UserTask.priority >= min_priority)
    rows = (await db.scalars(query.limit(limit))).all()
    return sorted(rows, key=_order_key)


def _order_key(task: UserTask):
    due = _aware(task.due_at) or datetime.max.replace(tzinfo=UTC)
    return (due, -task.priority, task.created_at)


def score(task: UserTask, tz: ZoneInfo, today: date, now: datetime) -> float:
    """How strongly a task belongs in today's plan: overdue and due today first, then
    priority, then soon-due, then age. Deterministic, so the plan is explainable."""
    value = task.priority * 10.0
    day = local_day(task.due_at, tz)
    if is_overdue(task, tz, now):
        value += 100
    elif day == today:
        value += 60
    elif day is not None and (day - today).days <= 3:
        value += 30 - (day - today).days * 5
    if task.status == "in_progress":
        value += 15
    age_days = (now - _aware(task.created_at)).days if task.created_at else 0
    return value + min(age_days, 30) * 0.2


async def briefing(
    db: AsyncSession, owner_id: uuid.UUID, settings: Settings, *, now: datetime | None = None
) -> dict:
    """Everything the day needs, without asking a model: overdue, today, upcoming,
    in progress, a suggested order, suggestions to review, Celius's open work, things
    waiting for the user. A calendar joins it when one is connected (Phase 6)."""
    from app.models import Memory, MemoryStatus, Skill, SkillVersion, ToolCall, ToolCallStatus

    now = now or utcnow()
    prefs = await user_prefs(db, owner_id, settings)
    tz = zone(prefs.timezone)
    today = now.astimezone(tz).date()
    tasks = await list_tasks(db, owner_id)
    by_id = {str(t.id): t for t in tasks}
    week_end = today + timedelta(days=7)
    overdue = [t for t in tasks if is_overdue(t, tz, now)]
    due_today = [t for t in tasks if t not in overdue and local_day(t.due_at, tz) == today]
    upcoming = [
        t
        for t in tasks
        if t not in overdue and (d := local_day(t.due_at, tz)) is not None and today < d <= week_end
    ]
    in_progress = [t for t in tasks if t.status == "in_progress"]
    ready = [t for t in tasks if not blocked(t, by_id)]
    plan = sorted(ready, key=lambda t: score(t, tz, today, now), reverse=True)[:8]
    suggestions = (
        await db.scalars(
            select(UserTask)
            .where(UserTask.owner_id == owner_id, UserTask.status == "suggested")
            .order_by(UserTask.created_at.desc())
            .limit(20)
        )
    ).all()
    agent = (
        await db.execute(
            select(AgentTask, Conversation.title)
            .join(Conversation, Conversation.id == AgentTask.conversation_id)
            .where(
                AgentTask.owner_id == owner_id,
                AgentTask.status.in_(OPEN_TASK_STATUSES),
                Conversation.deleted_at.is_(None),
            )
            .order_by(AgentTask.updated_at.desc())
            .limit(10)
        )
    ).all()
    approvals = len(
        (
            await db.scalars(
                select(ToolCall.id).where(
                    ToolCall.owner_id == owner_id,
                    ToolCall.status == ToolCallStatus.pending_approval,
                )
            )
        ).all()
    )
    memories = len(
        (
            await db.scalars(
                select(Memory.id).where(
                    Memory.owner_id == owner_id, Memory.status == MemoryStatus.proposed
                )
            )
        ).all()
    )
    skills = len(
        (
            await db.scalars(
                select(SkillVersion.id)
                .join(Skill, Skill.id == SkillVersion.skill_id)
                .where(Skill.owner_id == owner_id, SkillVersion.status == "pending")
            )
        ).all()
    )
    return {
        "date": today.isoformat(),
        "timezone": prefs.timezone,
        "overdue": overdue,
        "due_today": due_today,
        "upcoming": upcoming,
        "in_progress": in_progress,
        "plan": plan,
        "suggestions": list(suggestions),
        "blocked": [t for t in tasks if blocked(t, by_id)],
        "agent_tasks": [(task, title) for task, title in agent],
        "waiting": {"approvals": approvals, "memories": memories, "skills": skills},
        "calendar": None,
    }


def describe(task: UserTask, tz: ZoneInfo) -> str:
    """One line for the model."""
    parts = [task.title]
    if task.due_at:
        day = local_day(task.due_at, tz)
        when = (
            day.isoformat()
            if task.due_all_day
            else _aware(task.due_at).astimezone(tz).strftime("%Y-%m-%d %H:%M")
        )
        parts.append(f"due {when}")
    labels = {0: "low", 2: "high", 3: "urgent"}
    if task.priority in labels:
        parts.append(f"{labels[task.priority]} priority")
    if task.status == "in_progress":
        parts.append("in progress")
    if task.recurrence:
        parts.append(f"repeats {task.recurrence['freq']}")
    return f"[{str(task.id)[:8]}] " + ", ".join(parts)


def briefing_text(data: dict) -> str:
    tz = zone(data["timezone"])

    def section(name: str, items) -> list[str]:
        return [f"{name}:", *(f"- {describe(t, tz)}" for t in items)] if items else []

    lines = [f"Briefing for {data['date']} ({data['timezone']})."]
    lines += section("Overdue", data["overdue"])
    lines += section("Due today", data["due_today"])
    lines += section("In progress", data["in_progress"])
    lines += section("Next 7 days", data["upcoming"])
    lines += section("Suggested order for today", data["plan"])
    if data["blocked"]:
        lines.append(f"Waiting on other tasks: {len(data['blocked'])}")
    if data["suggestions"]:
        lines.append(f"Suggested tasks to review: {len(data['suggestions'])}")
    if data["agent_tasks"]:
        lines.append(
            "Celius's open work: "
            + "; ".join(f"{t.objective} ({t.status})" for t, _ in data["agent_tasks"])
        )
    waiting = data["waiting"]
    if any(waiting.values()):
        lines.append(
            f"Waiting for the user: {waiting['approvals']} approvals, "
            f"{waiting['memories']} memories and {waiting['skills']} skills to review"
        )
    lines.append("Calendar: not connected.")
    if len(lines) <= 2:
        lines.insert(1, "No open tasks.")
    return "\n".join(lines)


# ---------------------------------------------------------------------- reminders


async def fire_due_reminders(factory: SessionFactory, settings: Settings) -> int:
    """Send due reminders. Claimed with a conditional UPDATE, so several workers send
    each one once; a worker that was down sends missed ones when it comes back."""
    now = utcnow()
    async with factory() as db:
        due = (
            await db.scalars(
                select(UserTask.id)
                .where(
                    UserTask.remind_at.is_not(None),
                    UserTask.remind_at <= now,
                    UserTask.reminded_at.is_(None),
                    UserTask.status.in_(ACTIVE_TASK_STATUSES),
                )
                .limit(REMINDER_BATCH)
            )
        ).all()
        sent = 0
        for task_id in due:
            claimed = await db.execute(
                update(UserTask)
                .where(UserTask.id == task_id, UserTask.reminded_at.is_(None))
                .values(reminded_at=now)
                .returning(UserTask.id)
            )
            if claimed.scalar() is None:
                continue
            task = await db.get(UserTask, task_id)
            tz = zone((await user_prefs(db, task.owner_id, settings)).timezone)
            push.queue(
                db,
                task.owner_id,
                title="Reminder",
                body=describe(task, tz).split("] ", 1)[-1],
                url="/#/today",
                tag=f"task-{task.id}",
                urgency="high",
            )
            audit(db, task.owner_id, "user_task.reminded", "user_task", task.id)
            _changed(db, task)
            sent += 1
        await db.commit()
    return sent
