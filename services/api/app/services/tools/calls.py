"""Tool call records: serialization, approvals and expiry.

A run that needs approval pauses (`awaiting_approval`) and holds no worker. Each
decision is checked against the exact arguments (`args_hash`) and recorded in the audit
log. When the last pending call of a run is decided, the run is queued again and a worker
picks it up where it stopped.
"""

import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import Conversation, Run, RunStatus, ToolCall, ToolCallStatus, ToolGrant, User
from app.schemas.events import RunEventType
from app.schemas.tools import PendingApproval, ToolCallOut
from app.services.audit import audit
from app.services.runs import append_event
from app.services.sync import record_sync


def tool_call_out(call: ToolCall) -> ToolCallOut:
    return ToolCallOut(
        id=call.id,
        run_id=call.run_id,
        conversation_id=call.conversation_id,
        message_id=call.message_id,
        step=call.step,
        position=call.position,
        tool_name=call.tool_name,
        risk=call.risk,
        arguments=call.arguments,
        args_hash=call.args_hash,
        status=call.status,
        decision_source=call.decision_source,
        decision_reason=call.decision_reason,
        profile=call.profile,
        expires_at=call.expires_at,
        decided_at=call.decided_at,
        started_at=call.started_at,
        finished_at=call.finished_at,
        duration_ms=call.duration_ms,
        result=call.result,
        is_error=call.is_error,
        created_at=call.created_at,
    )


def call_event(call: ToolCall) -> dict:
    return {
        "message_id": str(call.message_id),
        "tool_call": tool_call_out(call).model_dump(mode="json"),
    }


async def calls_for_messages(
    db: AsyncSession, message_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ToolCallOut]]:
    if not message_ids:
        return {}
    rows = (
        await db.scalars(
            select(ToolCall)
            .where(ToolCall.message_id.in_(message_ids))
            .order_by(ToolCall.step, ToolCall.position)
        )
    ).all()
    grouped: dict[uuid.UUID, list[ToolCallOut]] = {}
    for row in rows:
        grouped.setdefault(row.message_id, []).append(tool_call_out(row))
    return grouped


async def pending_approvals(db: AsyncSession, owner_id: uuid.UUID) -> list[PendingApproval]:
    rows = (
        await db.execute(
            select(ToolCall, Conversation.title)
            .join(Conversation, Conversation.id == ToolCall.conversation_id)
            .where(
                ToolCall.owner_id == owner_id,
                ToolCall.status == ToolCallStatus.pending_approval,
                Conversation.deleted_at.is_(None),
            )
            .order_by(ToolCall.created_at)
        )
    ).all()
    return [
        PendingApproval(**tool_call_out(call).model_dump(), conversation_title=title)
        for call, title in rows
    ]


async def _owned_call(db: AsyncSession, owner_id: uuid.UUID, call_id: uuid.UUID) -> ToolCall:
    call = await db.scalar(
        select(ToolCall)
        .where(ToolCall.id == call_id, ToolCall.owner_id == owner_id)
        .with_for_update()
    )
    if call is None:
        raise not_found("Tool call")
    return call


def _require_pending(call: ToolCall) -> None:
    if call.status != ToolCallStatus.pending_approval:
        raise AppError(
            409, "not_pending", f"This call is already {call.status.value}", status=call.status
        )
    if call.expires_at is not None and call.expires_at < utcnow():
        raise AppError(409, "approval_expired", "This approval request has expired")


async def resume_if_decided(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """Queue the run again once none of its calls waits for a decision."""
    waiting = await db.scalar(
        select(func.count())
        .select_from(ToolCall)
        .where(ToolCall.run_id == run_id, ToolCall.status == ToolCallStatus.pending_approval)
    )
    if waiting:
        return False
    resumed = await db.execute(
        update(Run)
        .where(
            Run.id == run_id,
            Run.status == RunStatus.awaiting_approval,
            Run.cancel_requested.is_(False),
        )
        .values(status=RunStatus.queued)
        .returning(Run.id)
    )
    return resumed.scalar() is not None


async def _decided(db: AsyncSession, call: ToolCall) -> None:
    await append_event(db, call.run_id, RunEventType.tool_call_decided, call_event(call))
    record_sync(
        db,
        call.owner_id,
        "message",
        call.message_id,
        payload={"conversation_id": str(call.conversation_id)},
    )
    await resume_if_decided(db, call.run_id)


async def approve(
    db: AsyncSession,
    user: User,
    call_id: uuid.UUID,
    args_hash: str,
    *,
    remember: bool = False,
    ip_address: str | None = None,
) -> ToolCall:
    call = await _owned_call(db, user.id, call_id)
    _require_pending(call)
    if args_hash != call.args_hash:
        # The client approved something other than what will run.
        audit(
            db,
            user.id,
            "tool_call.approve_mismatch",
            "tool_call",
            call.id,
            ip_address,
            tool=call.tool_name,
        )
        await db.commit()
        raise AppError(
            409,
            "arguments_changed",
            "The arguments differ from what you approved; review the call again",
            args_hash=call.args_hash,
        )
    call.status = ToolCallStatus.approved
    call.decision_source = "user"
    call.decision_reason = "Approved for this conversation" if remember else "Approved once"
    call.decided_at = utcnow()
    if remember and await db.get(ToolGrant, (call.conversation_id, call.tool_name)) is None:
        db.add(
            ToolGrant(
                conversation_id=call.conversation_id, tool_name=call.tool_name, owner_id=user.id
            )
        )
    audit(
        db,
        user.id,
        "tool_call.approved",
        "tool_call",
        call.id,
        ip_address,
        tool=call.tool_name,
        args_hash=call.args_hash,
        remember=remember,
    )
    await _decided(db, call)
    return call


async def deny(
    db: AsyncSession,
    user: User,
    call_id: uuid.UUID,
    reason: str | None = None,
    *,
    ip_address: str | None = None,
) -> ToolCall:
    call = await _owned_call(db, user.id, call_id)
    _require_pending(call)
    call.status = ToolCallStatus.denied
    call.decision_source = "user"
    call.decision_reason = reason or "Denied"
    call.decided_at = utcnow()
    call.is_error = True
    audit(
        db,
        user.id,
        "tool_call.denied",
        "tool_call",
        call.id,
        ip_address,
        tool=call.tool_name,
        reason=reason,
    )
    await _decided(db, call)
    return call


async def expire_approvals(factory: SessionFactory) -> int:
    """Expire unanswered requests; the model is told the call didn't run."""
    async with factory() as db:
        expired = (
            await db.scalars(
                select(ToolCall)
                .where(
                    ToolCall.status == ToolCallStatus.pending_approval,
                    ToolCall.expires_at < utcnow(),
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for call in expired:
            call.status = ToolCallStatus.expired
            call.decision_source = "expiry"
            call.decision_reason = "Nobody approved it in time"
            call.decided_at = utcnow()
            call.is_error = True
            audit(db, call.owner_id, "tool_call.expired", "tool_call", call.id, tool=call.tool_name)
            await _decided(db, call)
        await db.commit()
        return len(expired)


def approval_expiry(settings: Settings):
    return utcnow() + timedelta(minutes=settings.tool_approval_ttl_minutes)
