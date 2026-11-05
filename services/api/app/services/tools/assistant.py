"""Personal-assistant tools: the user's task list and daily briefing (ADR C-10).

task_list, daily_briefing     read
task_add, task_update         write: they change the user's commitments, so the tool
                              policy decides whether to ask first (the Standard
                              profile asks; "Allow always in this chat" exists)
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.errors import AppError
from app.models import PRIORITIES, Conversation, UserTask
from app.services.assistant import tasks
from app.services.tools.registry import ToolContext, ToolOutput, ToolSpec, builtin

Priority = Literal["low", "normal", "high", "urgent"]
Repeat = Literal["daily", "weekdays", "weekly", "monthly", "yearly"]


def _error(message: str) -> ToolOutput:
    return ToolOutput(content=f"Error: {message}", data={"error": message}, is_error=True)


async def _find(db, ctx: ToolContext, ref: str) -> UserTask | None:
    """A task by its id or the 8-character prefix shown in lists."""
    ref = ref.strip().strip("[]")
    found = await tasks._owned_task(db, ctx.owner_id, ref)
    if found is not None or len(ref) < 6:
        return found
    for task in await tasks.list_tasks(
        db, ctx.owner_id, statuses=("suggested", "open", "in_progress", "done"), limit=500
    ):
        if str(task.id).startswith(ref):
            return task
    return None


class TaskAddArgs(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    due: str | None = Field(default=None, description="YYYY-MM-DD, or YYYY-MM-DDTHH:MM")
    priority: Priority = "normal"
    remind: str | None = Field(default=None, description="When to remind, YYYY-MM-DDTHH:MM")
    repeat: Repeat | None = None
    notes: str | None = Field(default=None, max_length=5000)
    parent: str | None = Field(default=None, description="id of the task this is part of")


async def _task_add(ctx: ToolContext, args: TaskAddArgs) -> ToolOutput:
    async with ctx.factory() as db:
        conversation = await db.get(Conversation, ctx.conversation_id)
        parent = None
        if args.parent:
            parent = await _find(db, ctx, args.parent)
            if parent is None:
                return _error(f"No task {args.parent!r}")
        try:
            task = await tasks.create(
                db,
                ctx.owner_id,
                title=args.title,
                settings=ctx.settings,
                notes=args.notes,
                priority=PRIORITIES[args.priority],
                due=args.due,
                remind=args.remind,
                recurrence={"freq": args.repeat} if args.repeat else None,
                project_id=conversation.project_id if conversation else None,
                parent_id=parent.id if parent else None,
                source="model",
                conversation_id=ctx.conversation_id,
            )
        except AppError as exc:
            return _error(exc.message)
        tz = tasks.zone((await tasks.user_prefs(db, ctx.owner_id, ctx.settings)).timezone)
        await db.commit()
    return ToolOutput(content=f"Added: {tasks.describe(task, tz)}", data={"task_id": str(task.id)})


builtin(
    ToolSpec(
        name="task_add",
        label="Add a task",
        description="Add a to-do to the user's task list (when they ask, or agree to it).",
        risk="write",
        handler=_task_add,
        args_model=TaskAddArgs,
    )
)


class TaskListArgs(BaseModel):
    status: Literal["active", "done", "suggested", "all"] = "active"
    query: str | None = Field(default=None, max_length=200)
    due_within_days: int | None = Field(default=None, ge=0, le=366)
    min_priority: Priority | None = None


async def _task_list(ctx: ToolContext, args: TaskListArgs) -> ToolOutput:
    from datetime import timedelta

    from app.db.base import utcnow

    statuses = {
        "active": ("open", "in_progress"),
        "done": ("done",),
        "suggested": ("suggested",),
        "all": ("suggested", "open", "in_progress", "done", "cancelled"),
    }[args.status]
    async with ctx.factory() as db:
        conversation = await db.get(Conversation, ctx.conversation_id)
        found = await tasks.list_tasks(
            db,
            ctx.owner_id,
            statuses=statuses,
            q=args.query,
            due_before=utcnow() + timedelta(days=args.due_within_days + 1)
            if args.due_within_days is not None
            else None,
            min_priority=PRIORITIES[args.min_priority] if args.min_priority else None,
            limit=60,
        )
        tz = tasks.zone((await tasks.user_prefs(db, ctx.owner_id, ctx.settings)).timezone)
    if not found:
        return ToolOutput(content="No matching tasks.", data={"count": 0})
    project = conversation.project_id if conversation else None
    lines = []
    for task in found:
        overdue = " (OVERDUE)" if tasks.is_overdue(task, tz) else ""
        here = " (this project)" if project and task.project_id == project else ""
        lines.append(f"- {tasks.describe(task, tz)}{overdue}{here}")
    return ToolOutput(content="\n".join(lines), data={"count": len(found)})


builtin(
    ToolSpec(
        name="task_list",
        label="List tasks",
        description="The user's to-dos: filter by status, words, due date or priority.",
        risk="read",
        handler=_task_list,
        args_model=TaskListArgs,
    )
)


class TaskUpdateArgs(BaseModel):
    task: str = Field(min_length=6, max_length=40, description="The task's id (from task_list)")
    status: Literal["open", "in_progress", "done", "cancelled"] | None = None
    title: str | None = Field(default=None, max_length=300)
    due: str | None = Field(default=None, description="YYYY-MM-DD; empty string clears it")
    priority: Priority | None = None
    remind: str | None = None
    notes: str | None = Field(default=None, max_length=5000)


async def _task_update(ctx: ToolContext, args: TaskUpdateArgs) -> ToolOutput:
    changes = args.model_dump(exclude_none=True, exclude={"task"})
    if "priority" in changes:
        changes["priority"] = PRIORITIES[changes["priority"]]
    if not changes:
        return _error("Nothing to change")
    async with ctx.factory() as db:
        task = await _find(db, ctx, args.task)
        if task is None:
            return _error(f"No task {args.task!r}; use task_list to find it")
        try:
            following = await tasks.change(db, task, changes, ctx.settings, actor="model")
        except AppError as exc:
            return _error(exc.message)
        tz = tasks.zone((await tasks.user_prefs(db, ctx.owner_id, ctx.settings)).timezone)
        await db.commit()
    text = f"Updated: {tasks.describe(task, tz)} [{task.status}]"
    if following is not None:
        text += f"\nNext occurrence: {tasks.describe(following, tz)}"
    return ToolOutput(content=text, data={"task_id": str(task.id), "status": task.status})


builtin(
    ToolSpec(
        name="task_update",
        label="Update a task",
        description="Mark a task done or in progress, or change it. Only change dates, "
        "priority or cancel when the user asks.",
        risk="write",
        handler=_task_update,
        args_model=TaskUpdateArgs,
    )
)


class BriefingArgs(BaseModel):
    pass


async def _daily_briefing(ctx: ToolContext, args: BriefingArgs) -> ToolOutput:
    async with ctx.factory() as db:
        data = await tasks.briefing(db, ctx.owner_id, ctx.settings)
    return ToolOutput(
        content=tasks.briefing_text(data),
        data={"date": data["date"], "overdue": len(data["overdue"])},
    )


builtin(
    ToolSpec(
        name="daily_briefing",
        label="Daily briefing",
        description="Today's overdue, due and upcoming tasks, a suggested order, and what "
        "waits for the user. Use it to plan the day.",
        risk="read",
        handler=_daily_briefing,
        args_model=BriefingArgs,
        optional=True,  # lite: task_list covers it
    )
)

ASSISTANT_NOTE = (
    "The user keeps a task list and calendar in Celius: use the task and calendar tools when "
    "they talk about to-dos, deadlines, appointments, reminders or planning, and "
    "daily_briefing to plan their day. Don't change dates, priorities or events, cancel "
    "anything, or send email, unless they ask."
)
