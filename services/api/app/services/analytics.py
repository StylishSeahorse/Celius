"""Execution analytics (ADR C-12): how Celius performs, computed from what it already
records. No extra logging, no model. For one person, or (administrators) everyone.

  runs        completion/failure/cancel rates, failure reasons, duration p50/p90, tool
              steps, replies that ran out of tool steps
  tools       per tool: calls, success rate, asked/denied, duration p50
  context     estimated prompt tokens p50/p90, tokens per prompt section, dropped history,
              by profile (from run.started)
  agent       task completion, verified completion, recovery (interrupted → completed)
  skills      reuse rate (replies that loaded a skill / replies with tools), use success,
              test pass rate
  memory      memories given per reply, proposal acceptance, open conflicts
  assistant   tasks added/completed, suggestions accepted, automations ok/failed

Rates are None when there is nothing to divide by, so "no data" never reads as 0%.
"""

import statistics
import uuid
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import (
    AgentTask,
    AgentTaskEvent,
    Automation,
    AutomationRun,
    Memory,
    MemoryConflict,
    MemoryStatus,
    Message,
    MessagePart,
    MessageRole,
    Run,
    RunEvent,
    RunStatus,
    Skill,
    SkillEvent,
    ToolCall,
    ToolCallStatus,
    UserTask,
)

MAX_ROWS = 50_000


def rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)
    return round(ordered[index], 1)


def _seconds(start, end) -> float | None:
    if start is None or end is None:
        return None
    if start.tzinfo is None and end.tzinfo is not None:
        start = start.replace(tzinfo=end.tzinfo)
    if end.tzinfo is None and start.tzinfo is not None:
        end = end.replace(tzinfo=start.tzinfo)
    return max((end - start).total_seconds(), 0.0)


async def compute(db: AsyncSession, owner_id: uuid.UUID | None, days: int) -> dict:
    since = utcnow() - timedelta(days=days)

    def mine(column):
        return [column == owner_id] if owner_id is not None else []

    # ------------------------------------------------------------------ runs
    runs = (
        await db.execute(
            select(
                Run.id,
                Run.status,
                Run.error_code,
                Run.started_at,
                Run.finished_at,
                Run.tool_iterations,
                Run.params,
            )
            .where(Run.created_at >= since, *mine(Run.owner_id))
            .limit(MAX_ROWS)
        )
    ).all()
    finished = [
        r for r in runs if r.status in (RunStatus.completed, RunStatus.failed, RunStatus.cancelled)
    ]
    durations = [d for r in finished if (d := _seconds(r.started_at, r.finished_at)) is not None]
    with_tools = {r.id for r in runs if (r.tool_iterations or 0) > 0}
    run_section = {
        "total": len(runs),
        "completed": sum(r.status == RunStatus.completed for r in runs),
        "completion_rate": rate(
            sum(r.status == RunStatus.completed for r in finished), len(finished)
        ),
        "failure_rate": rate(sum(r.status == RunStatus.failed for r in finished), len(finished)),
        "cancel_rate": rate(sum(r.status == RunStatus.cancelled for r in finished), len(finished)),
        "failure_reasons": dict(
            Counter(
                r.error_code or "unknown" for r in runs if r.status == RunStatus.failed
            ).most_common(8)
        ),
        "duration_seconds_p50": percentile(durations, 0.5),
        "duration_seconds_p90": percentile(durations, 0.9),
        "with_tools": len(with_tools),
        "tool_steps_mean": round(statistics.mean([r.tool_iterations or 0 for r in runs]), 2)
        if runs
        else None,
        "scheduled_or_automated": sum((r.params or {}).get("source") == "schedule" for r in runs),
    }

    # ------------------------------------------------------------------ tools
    calls = (
        await db.execute(
            select(
                ToolCall.tool_name,
                ToolCall.status,
                ToolCall.is_error,
                ToolCall.duration_ms,
                ToolCall.decision_source,
            )
            .where(ToolCall.created_at >= since, *mine(ToolCall.owner_id))
            .limit(MAX_ROWS)
        )
    ).all()
    per_tool: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "ok": 0, "failed": 0, "asked": 0, "denied": 0, "durations": []}
    )
    for call in calls:
        stats = per_tool[call.tool_name]
        stats["calls"] += 1
        if call.status == ToolCallStatus.completed and not call.is_error:
            stats["ok"] += 1
        elif call.status in (ToolCallStatus.failed, ToolCallStatus.interrupted) or call.is_error:
            stats["failed"] += 1
        if call.decision_source == "user":
            stats["asked"] += 1
        if call.status in (ToolCallStatus.denied, ToolCallStatus.expired):
            stats["asked"] += 1
            stats["denied"] += 1
        if call.duration_ms:
            stats["durations"].append(call.duration_ms)
    executed = sum(s["ok"] + s["failed"] for s in per_tool.values())
    tool_section = {
        "calls": len(calls),
        "success_rate": rate(sum(s["ok"] for s in per_tool.values()), executed),
        "by_tool": sorted(
            (
                {
                    "tool": name,
                    "calls": s["calls"],
                    "success_rate": rate(s["ok"], s["ok"] + s["failed"]),
                    "asked": s["asked"],
                    "denied": s["denied"],
                    "duration_ms_p50": percentile(s["durations"], 0.5),
                }
                for name, s in per_tool.items()
            ),
            key=lambda item: -item["calls"],
        ),
    }

    # ------------------------------------------------------------------ context
    started = (
        (
            await db.execute(
                select(RunEvent.data)
                .where(
                    RunEvent.type == "run.started",
                    RunEvent.run_id.in_(
                        select(Run.id).where(Run.created_at >= since, *mine(Run.owner_id))
                    ),
                )
                .limit(MAX_ROWS)
            )
        )
        .scalars()
        .all()
    )
    tokens = [
        d.get("estimated_prompt_tokens")
        for d in started
        if isinstance(d.get("estimated_prompt_tokens"), int)
    ]
    sections: dict[str, list[int]] = defaultdict(list)
    profiles = Counter()
    for data in started:
        profiles[data.get("profile") or "unknown"] += 1
        for name, value in (data.get("prompt_sections") or {}).items():
            if isinstance(value, int):
                sections[name].append(value)
    context_section = {
        "prompt_tokens_p50": percentile(tokens, 0.5),
        "prompt_tokens_p90": percentile(tokens, 0.9),
        "dropped_history_rate": rate(
            sum(1 for d in started if (d.get("dropped_messages") or 0) > 0), len(started)
        ),
        "section_tokens_mean": {
            name: round(statistics.mean(v)) for name, v in sorted(sections.items())
        },
        "profiles": dict(profiles),
    }

    # ------------------------------------------------------------------ agent tasks
    tasks = (
        await db.execute(
            select(AgentTask.id, AgentTask.status, AgentTask.verifications)
            .where(AgentTask.created_at >= since, *mine(AgentTask.owner_id))
            .limit(MAX_ROWS)
        )
    ).all()
    interrupted = (
        set(
            (
                await db.scalars(
                    select(AgentTaskEvent.task_id).where(
                        AgentTaskEvent.task_id.in_([t.id for t in tasks]),
                        AgentTaskEvent.kind == "status",
                        AgentTaskEvent.summary.like("% → paused%"),
                    )
                )
            ).all()
        )
        if tasks
        else set()
    )
    completed = [t for t in tasks if t.status == "completed"]

    def verified(row) -> bool:
        latest: dict[str, bool] = {}
        for item in row.verifications or []:
            latest[item.get("check")] = bool(item.get("passed"))
        return bool(latest) and all(latest.values())

    agent_section = {
        "tasks": len(tasks),
        "completion_rate": rate(
            len(completed), sum(t.status in ("completed", "failed", "abandoned") for t in tasks)
        ),
        "verified_completion_rate": rate(sum(verified(t) for t in completed), len(completed)),
        "interrupted": len(interrupted),
        "recovery_rate": rate(
            sum(t.id in interrupted and t.status == "completed" for t in tasks), len(interrupted)
        ),
    }

    # ------------------------------------------------------------------ skills
    uses = (
        await db.execute(
            select(SkillEvent.kind, SkillEvent.outcome, SkillEvent.run_id)
            .join(Skill, Skill.id == SkillEvent.skill_id)
            .where(
                SkillEvent.created_at >= since,
                SkillEvent.kind.in_(["use", "test"]),
                *mine(Skill.owner_id),
            )
            .limit(MAX_ROWS)
        )
    ).all()
    use_rows = [u for u in uses if u.kind == "use"]
    settled = [u for u in use_rows if u.outcome in ("success", "failure")]
    tests = [u for u in uses if u.kind == "test"]
    skill_section = {
        "uses": len(use_rows),
        "reuse_rate": rate(
            len({u.run_id for u in use_rows if u.run_id in with_tools}), len(with_tools)
        ),
        "use_success_rate": rate(sum(u.outcome == "success" for u in settled), len(settled)),
        "test_pass_rate": rate(sum(u.outcome == "passed" for u in tests), len(tests)),
        "skills": await db.scalar(
            select(func.count())
            .select_from(Skill)
            .where(Skill.status == "active", *mine(Skill.owner_id))
        ),
    }

    # ------------------------------------------------------------------ memory
    replies = await db.scalar(
        select(func.count())
        .select_from(Message)
        .join(Run, Run.output_message_id == Message.id)
        .where(Message.role == MessageRole.assistant, Run.created_at >= since, *mine(Run.owner_id))
    )
    with_memory = await db.scalar(
        select(func.count(func.distinct(MessagePart.message_id)))
        .select_from(MessagePart)
        .join(Run, Run.output_message_id == MessagePart.message_id)
        .where(MessagePart.type == "memory", Run.created_at >= since, *mine(Run.owner_id))
    )
    proposals = (
        await db.execute(
            select(Memory.status, Memory.reviewed_at)
            .where(
                Memory.created_at >= since,
                Memory.source.in_(["extraction", "model"]),
                *mine(Memory.owner_id),
            )
            .limit(MAX_ROWS)
        )
    ).all()
    reviewed = [p for p in proposals if p.status != MemoryStatus.proposed]
    memory_section = {
        "replies_given_memory_rate": rate(with_memory or 0, replies or 0),
        "proposals": len(proposals),
        "acceptance_rate": rate(
            sum(p.status in (MemoryStatus.active, MemoryStatus.superseded) for p in reviewed),
            len(reviewed),
        ),
        "waiting_review": sum(p.status == MemoryStatus.proposed for p in proposals),
        "open_conflicts": await db.scalar(
            select(func.count())
            .select_from(MemoryConflict)
            .where(MemoryConflict.status == "open", *mine(MemoryConflict.owner_id))
        ),
        "never_used": await db.scalar(
            select(func.count())
            .select_from(Memory)
            .where(
                Memory.status == MemoryStatus.active, Memory.use_count == 0, *mine(Memory.owner_id)
            )
        ),
    }

    # ------------------------------------------------------------------ assistant
    user_tasks = (
        await db.execute(
            select(UserTask.status, UserTask.source)
            .where(UserTask.created_at >= since, *mine(UserTask.owner_id))
            .limit(MAX_ROWS)
        )
    ).all()
    suggested = [t for t in user_tasks if t.source == "suggested"]
    automation_runs = (
        await db.scalars(
            select(AutomationRun.status)
            .join(Automation, Automation.id == AutomationRun.automation_id)
            .where(AutomationRun.created_at >= since, *mine(Automation.owner_id))
            .limit(MAX_ROWS)
        )
    ).all()
    assistant_section = {
        "tasks_added": sum(t.status != "suggested" for t in user_tasks),
        "tasks_completed": sum(t.status == "done" for t in user_tasks),
        "suggestions": len(suggested),
        "suggestions_accepted": sum(t.status != "suggested" for t in suggested),
        "automation_runs": len(automation_runs),
        "automation_success_rate": rate(
            sum(s == "ok" for s in automation_runs),
            sum(s in ("ok", "failed") for s in automation_runs),
        ),
    }

    return {
        "days": days,
        "scope": "everyone" if owner_id is None else "me",
        "runs": run_section,
        "tools": tool_section,
        "context": context_section,
        "agent": agent_section,
        "skills": skill_section,
        "memory": memory_section,
        "assistant": assistant_section,
    }
