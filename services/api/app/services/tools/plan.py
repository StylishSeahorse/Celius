"""Plan tools: the model's side of the task state (services/agent/tasks.py).

They change only Celius's record of the work, never files or the outside world, so they
are `read` risk like `remember`. The state they keep is given back to the model at the
start of every reply in the conversation.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.models import Conversation
from app.services.agent import tasks
from app.services.tools.registry import ToolContext, ToolOutput, ToolSpec, builtin


def _error(message: str) -> ToolOutput:
    return ToolOutput(content=f"Error: {message}", data={"error": message}, is_error=True)


def _state(task) -> ToolOutput:
    return ToolOutput(
        content=tasks.render(task),
        data={"task_id": str(task.id), "status": task.status},
    )


class PlanStartArgs(BaseModel):
    objective: str = Field(min_length=3, max_length=2000)
    steps: list[str] = Field(min_length=1, max_length=40)
    replace: bool = Field(default=False, description="Abandon the open task")


async def _plan_start(ctx: ToolContext, args: PlanStartArgs) -> ToolOutput:
    async with ctx.factory() as db:
        conversation = await db.get(Conversation, ctx.conversation_id)
        if conversation is None:
            return _error("Conversation not found")
        try:
            task = await tasks.start(
                db,
                conversation,
                args.objective,
                args.steps,
                run_id=ctx.run_id,
                replace=args.replace,
            )
        except tasks.TaskError as exc:
            return _error(str(exc))
        await db.commit()
    return _state(task)


builtin(
    ToolSpec(
        name="plan_start",
        label="Start a plan",
        description="Record the objective and steps of a multi-step task before working on it.",
        risk="read",
        handler=_plan_start,
        args_model=PlanStartArgs,
    )
)


class PlanUpdateArgs(BaseModel):
    step: int | None = Field(default=None, ge=1)
    step_status: Literal["in_progress", "done", "failed", "skipped", "pending"] | None = None
    step_note: str | None = Field(default=None, max_length=500)
    add_steps: list[str] = Field(default_factory=list, max_length=20)
    note: str | None = Field(default=None, max_length=500, description="A finding to keep")
    blocker: str | None = Field(default=None, max_length=500)
    clear_blockers: bool = False
    check: str | None = Field(default=None, max_length=300, description="A check you ran")
    passed: bool | None = None
    status: Literal["completed", "failed", "blocked", "active"] | None = Field(
        default=None, description="The whole task's status"
    )


async def _plan_update(ctx: ToolContext, args: PlanUpdateArgs) -> ToolOutput:
    if args.check and args.passed is None:
        return _error("Say whether the check passed (passed: true or false)")
    async with ctx.factory() as db:
        task = await tasks.open_task(db, ctx.conversation_id)
        if task is None:
            return _error("No open task in this chat; start one with plan_start")
        try:
            said = tasks.apply(
                db,
                task,
                tasks.Update(
                    step=args.step,
                    step_status=args.step_status,
                    step_note=args.step_note,
                    add_steps=args.add_steps,
                    note=args.note,
                    blocker=args.blocker,
                    clear_blockers=args.clear_blockers,
                    check=args.check,
                    passed=args.passed,
                    status=args.status,
                ),
                run_id=ctx.run_id,
            )
        except tasks.TaskError as exc:
            await db.rollback()
            return _error(str(exc))
        await db.commit()
    return ToolOutput(
        content=" ".join(said) or "Nothing changed.",
        data={"task_id": str(task.id), "status": task.status},
    )


builtin(
    ToolSpec(
        name="plan_update",
        label="Update the plan",
        description="Update the open task: step status/note, new steps, a finding, a blocker, "
        "a check and whether it passed, or the task's status.",
        risk="read",
        handler=_plan_update,
        args_model=PlanUpdateArgs,
    )
)


class PlanStatusArgs(BaseModel):
    pass


async def _plan_status(ctx: ToolContext, args: PlanStatusArgs) -> ToolOutput:
    async with ctx.factory() as db:
        task = await tasks.open_task(db, ctx.conversation_id)
    if task is None:
        return ToolOutput(content="No open task in this chat.", data={})
    return _state(task)


builtin(
    ToolSpec(
        name="plan_status",
        label="Plan status",
        description="Show the open task's current state.",
        risk="read",
        handler=_plan_status,
        args_model=PlanStatusArgs,
        optional=True,  # the state is in the system prompt already
    )
)

PLAN_TOOLS = ("plan_start", "plan_update", "plan_status")
