"""External agent sessions (ADR C-16, mode 2) projected into the conversation.

An external agent (Claude Code) runs the turn on the machine. Its canonical events become
what the chat already shows for Celius's own replies:

  turn.started        run → running, run.started
  assistant.text      a text part + message.delta
  assistant.reasoning a reasoning part + message.delta
  tool.started        a tool_calls row (running, decision_source "external") + events
  permission.request  that row → pending_approval, run → awaiting_approval, a push; the
                      user decides with the usual approve/deny routes, and the decision
                      goes back to the machine (see on_decision)
  tool.completed      the row → completed / failed
  file.changed        kept in the session's event log (files view)
  turn.completed      message complete, run completed (or failed)

The projection runs once per stored event (ingest.py), in the same transaction.
"""

import hashlib
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import utcnow
from app.models import (
    TERMINAL_RUN_STATUSES,
    Conversation,
    Message,
    MessagePart,
    MessageStatus,
    PushSubscription,
    RemoteSession,
    RemoteSessionEvent,
    Run,
    RunStatus,
    ToolCall,
    ToolCallStatus,
)
from app.schemas.events import RunEventType
from app.services import push
from app.services.audit import audit
from app.services.runs import append_event
from app.services.sync import record_sync
from app.services.tools.calls import call_event
from app.services.tools.policy import args_hash

# Claude Code's tools, by what they can do (for the approval card and the audit trail).
RISK = {
    "Read": "read",
    "Glob": "read",
    "Grep": "read",
    "LS": "read",
    "TodoWrite": "read",
    "Task": "read",
    "Write": "write",
    "Edit": "write",
    "MultiEdit": "write",
    "NotebookEdit": "write",
    "Bash": "execute",
    "BashOutput": "read",
    "KillShell": "execute",
    "WebFetch": "network",
    "WebSearch": "network",
}
PROFILE = "external:claude_code"


def _risk(tool: str) -> str:
    if tool.startswith("mcp__"):
        return "external"
    return RISK.get(tool, "execute")


def _call_key(tool_use_id: str) -> str:
    return "x" + hashlib.sha256(tool_use_id.encode()).hexdigest()[:31]


def _arguments(value) -> dict:
    return value if isinstance(value, dict) else {"value": value}


async def _run(db: AsyncSession, session: RemoteSession, event: RemoteSessionEvent) -> Run | None:
    run_id = event.run_id or session.current_run_id
    if run_id is None:
        return None
    run = await db.get(Run, run_id)
    if run is None or run.remote_session_id != session.id or run.execution != "external":
        return None
    return run


async def _next_position(db: AsyncSession, message: Message) -> int:
    current = await db.scalar(
        select(func.max(MessagePart.position)).where(
            MessagePart.message_id == message.id,
            MessagePart.revision == message.current_revision,
        )
    )
    return (current if current is not None else -1) + 1


async def _sync_message(db: AsyncSession, run: Run) -> None:
    record_sync(
        db,
        run.owner_id,
        "message",
        run.output_message_id,
        payload={"conversation_id": str(run.conversation_id)},
    )


async def _queue_push(db: AsyncSession, run: Run, body: str, urgency: str = "normal") -> None:
    subscribed = await db.scalar(
        select(PushSubscription.id).where(PushSubscription.user_id == run.owner_id).limit(1)
    )
    if subscribed is None:
        return
    conversation = await db.get(Conversation, run.conversation_id)
    push.queue(
        db,
        run.owner_id,
        title=(conversation.title if conversation and conversation.title else None) or "Celius",
        body=body[:180] or "Your reply is ready.",
        url=f"/#/c/{run.conversation_id}",
        tag=str(run.conversation_id),
        urgency=urgency,
    )


async def _find_call(db: AsyncSession, run: Run, tool_use_id: str | None) -> ToolCall | None:
    if not tool_use_id:
        return None
    return await db.scalar(
        select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.model_call_id == tool_use_id)
    )


async def _new_call(
    db: AsyncSession, run: Run, message: Message, tool_use_id: str, tool: str, arguments: dict
) -> ToolCall:
    step = (
        await db.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run.id))
        or 0
    )
    arguments = _arguments(arguments)
    call = ToolCall(
        id=uuid.uuid4(),
        owner_id=run.owner_id,
        run_id=run.id,
        conversation_id=run.conversation_id,
        message_id=message.id,
        step=step,
        position=0,
        call_key=_call_key(tool_use_id),
        model_call_id=tool_use_id[:128],
        tool_name=(tool or "(unknown)")[:200],
        risk=_risk(tool),
        arguments=arguments,
        args_hash=args_hash(arguments),
        status=ToolCallStatus.running,
        decision_source="external",
        decision_reason="Allowed by the agent's own permission settings",
        profile=PROFILE,
    )
    db.add(call)
    await db.flush()
    db.add(
        MessagePart(
            message_id=message.id,
            revision=message.current_revision,
            position=await _next_position(db, message),
            type="tool_call",
            data={"tool_call_id": str(call.id), "step": step},
        )
    )
    await append_event(db, run.id, RunEventType.tool_call_proposed, call_event(call))
    return call


async def project(db: AsyncSession, session: RemoteSession, event: RemoteSessionEvent) -> None:
    data = event.data
    if event.type == "session.started":
        if data.get("external_session_id"):
            session.external_session_id = str(data["external_session_id"])[:128]
        return
    run = await _run(db, session, event)
    if run is None or run.status in TERMINAL_RUN_STATUSES:
        return  # e.g. events still arriving after the user stopped the reply
    message = await db.get(Message, run.output_message_id)
    if message is None:
        return

    if event.type == "turn.started":
        if run.status == RunStatus.queued:
            run.status = RunStatus.running
            run.started_at = run.started_at or utcnow()
            run.attempts += 1
            message.status = MessageStatus.streaming
            await append_event(
                db,
                run.id,
                RunEventType.run_started,
                {
                    "message_id": str(message.id),
                    "model_id": run.model_id,
                    "attempt": run.attempts,
                    "external_agent": session.adapter,
                },
            )
        return

    if event.type in ("assistant.text", "assistant.reasoning"):
        text = str(data.get("text") or "")
        if not text:
            return
        kind = "text" if event.type == "assistant.text" else "reasoning"
        db.add(
            MessagePart(
                message_id=message.id,
                revision=message.current_revision,
                position=await _next_position(db, message),
                type=kind,
                text=text,
                data={"step": event.seq},
            )
        )
        await append_event(
            db, run.id, RunEventType.message_delta, {"message_id": str(message.id), kind: text}
        )
        return

    if event.type == "tool.started":
        tool_use_id = str(data.get("tool_use_id") or f"seq-{event.seq}")
        call = await _find_call(db, run, tool_use_id)
        if call is None:
            call = await _new_call(
                db, run, message, tool_use_id, str(data.get("name") or ""), data.get("input")
            )
        if call.status in (ToolCallStatus.running, ToolCallStatus.approved):
            call.status = ToolCallStatus.running
            call.started_at = call.started_at or utcnow()
            await append_event(db, run.id, RunEventType.tool_call_started, call_event(call))
        await _sync_message(db, run)
        return

    if event.type == "permission.request":
        tool_use_id = str(data.get("tool_use_id") or f"perm-{data.get('request_id')}")
        call = await _find_call(db, run, tool_use_id)
        if call is None:
            call = await _new_call(
                db, run, message, tool_use_id, str(data.get("tool") or ""), data.get("input")
            )
        settings = get_settings()
        call.status = ToolCallStatus.pending_approval
        call.decision_source = None
        call.decision_reason = str(data.get("reason") or "")[:500] or None
        call.decided_at = None
        call.expires_at = utcnow() + timedelta(minutes=settings.tool_approval_ttl_minutes)
        # Where to send the decision. Replaced by the tool's result once it runs.
        call.result = {"permission_request_id": str(data.get("request_id") or "")}
        run.status = RunStatus.awaiting_approval
        await append_event(db, run.id, RunEventType.tool_call_awaiting_approval, call_event(call))
        await append_event(
            db,
            run.id,
            RunEventType.run_progress,
            {"stage": "awaiting_approval", "message_id": str(message.id)},
        )
        await _queue_push(db, run, f"Waiting for your approval: {call.tool_name}", "high")
        await _sync_message(db, run)
        return

    if event.type == "tool.completed":
        call = await _find_call(db, run, str(data.get("tool_use_id") or ""))
        if call is None:
            return
        is_error = bool(data.get("is_error"))
        content = str(data.get("content") or "")
        if call.status in (ToolCallStatus.running, ToolCallStatus.approved):
            call.status = ToolCallStatus.failed if is_error else ToolCallStatus.completed
        call.is_error = is_error or call.is_error
        call.model_content = content[:20_000]
        call.result = {"content": content[:20_000]}
        call.finished_at = utcnow()
        if call.started_at is not None:
            started = (
                call.started_at
                if call.started_at.tzinfo
                else call.started_at.replace(tzinfo=utcnow().tzinfo)
            )
            call.duration_ms = int((utcnow() - started).total_seconds() * 1000)
        await append_event(db, run.id, RunEventType.tool_call_completed, call_event(call))
        await _sync_message(db, run)
        return

    if event.type == "turn.completed":
        if data.get("external_session_id"):
            session.external_session_id = str(data["external_session_id"])[:128]
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if data.get("total_cost_usd") is not None:
            usage = {**usage, "total_cost_usd": data["total_cost_usd"]}
        run.usage = usage or None
        run.finished_at = utcnow()
        run.lease_expires_at = None
        await _close_waiting_calls(db, run)
        if data.get("is_error"):
            error = str(data.get("result") or data.get("subtype") or "The agent stopped")
            run.status = RunStatus.failed
            run.error_code = "external_agent_error"
            run.error_message = error[:2000]
            message.status = MessageStatus.failed
            await append_event(
                db,
                run.id,
                RunEventType.run_failed,
                {
                    "message_id": str(message.id),
                    "code": "external_agent_error",
                    "message": error[:500],
                    "retryable": False,
                },
            )
            await _queue_push(db, run, f"The reply failed: {error}")
        else:
            has_text = await db.scalar(
                select(func.count())
                .select_from(MessagePart)
                .where(MessagePart.message_id == message.id, MessagePart.type == "text")
            )
            if not has_text and data.get("result"):
                db.add(
                    MessagePart(
                        message_id=message.id,
                        revision=message.current_revision,
                        position=await _next_position(db, message),
                        type="text",
                        text=str(data["result"]),
                    )
                )
            run.status = RunStatus.completed
            message.status = MessageStatus.complete
            await append_event(
                db,
                run.id,
                RunEventType.run_completed,
                {"message_id": str(message.id), "usage": run.usage, "finish_reason": "stop"},
            )
            await _queue_push(db, run, str(data.get("result") or "Done."))
        await _sync_message(db, run)
        return


async def _close_waiting_calls(db: AsyncSession, run: Run) -> None:
    """At the end of a turn nothing can still be waiting or running."""
    open_calls = (
        await db.scalars(
            select(ToolCall).where(
                ToolCall.run_id == run.id,
                ToolCall.status.in_(
                    [
                        ToolCallStatus.pending_approval,
                        ToolCallStatus.approved,
                        ToolCallStatus.running,
                    ]
                ),
            )
        )
    ).all()
    for call in open_calls:
        call.status = ToolCallStatus.cancelled
        call.finished_at = utcnow()
        call.decision_reason = call.decision_reason or "The agent's turn ended"


# ---------------------------------------------------------------------- user decisions


async def on_decision(db: AsyncSession, call: ToolCall) -> bool:
    """Called by the approval code when a call is decided. For an external agent's call:
    send the decision to the machine and put the run back to running (no worker claims
    it). Returns True when the call belonged to an external agent."""
    from app.services.remote.sessions import enqueue_command

    run = await db.get(Run, call.run_id)
    if run is None or run.execution != "external" or run.remote_session_id is None:
        return False
    session = await db.get(RemoteSession, run.remote_session_id)
    request_id = (call.result or {}).get("permission_request_id")
    if session is None or session.agent_id is None or not request_id:
        return True
    allowed = call.status == ToolCallStatus.approved
    enqueue_command(
        db,
        session.agent_id,
        "permission.decision",
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        payload={
            "request_id": request_id,
            "behavior": "allow" if allowed else "deny",
            "message": None if allowed else (call.decision_reason or "Denied in Celius"),
        },
    )
    if allowed:
        call.status = ToolCallStatus.running  # the agent runs it now
        call.started_at = utcnow()
    audit(
        db,
        run.owner_id,
        "remote_permission.decided",
        "tool_call",
        call.id,
        decision="allow" if allowed else "deny",
        tool=call.tool_name,
    )
    waiting = await db.scalar(
        select(func.count())
        .select_from(ToolCall)
        .where(ToolCall.run_id == run.id, ToolCall.status == ToolCallStatus.pending_approval)
    )
    if not waiting and run.status == RunStatus.awaiting_approval:
        run.status = RunStatus.running
    return True


async def cancel(db: AsyncSession, run: Run) -> None:
    """The user stopped an external agent's reply: interrupt it on the machine."""
    from app.services.remote.sessions import enqueue_command

    session = await db.get(RemoteSession, run.remote_session_id) if run.remote_session_id else None
    if session is not None and session.agent_id is not None:
        enqueue_command(db, session.agent_id, "interrupt", session_id=session.id, run_id=run.id)
