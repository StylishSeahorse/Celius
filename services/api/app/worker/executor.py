"""Executes generation runs claimed from PostgreSQL.

Lifecycle of a run:

    queued ──claim──▶ running ──▶ completed | failed | cancelled
                        │
                        └─ lease expired (worker died) ──▶ queued again (bounded by
                           RUN_MAX_ATTEMPTS), with a `run.progress {stage: restarted}`
                           event telling clients to discard the partial output.

The worker holds a lease on each run and renews it while streaming (and on a timer, for
long prompt processing where no tokens arrive). Every renewal also reads the run's
`cancel_requested` flag, so cancellation takes effect within about a second, even when
the model is still processing the prompt.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.errors import AppError
from app.models import (
    Conversation,
    Message,
    MessagePart,
    MessageRole,
    MessageStatus,
    Project,
    Run,
    RunStatus,
)
from app.schemas.events import RunEventType
from app.services.context import HistoryItem, build_context, context_budget
from app.services.conversations import load_parts, message_ancestry, message_text
from app.services.model_gateway import ChatRequest, ModelGateway, ModelGatewayError
from app.services.runs import append_event, finish_cancelled
from app.services.sync import record_sync

log = logging.getLogger(__name__)

HEARTBEAT_MAX_INTERVAL_SECONDS = 1.0


# ---------------------------------------------------------------------- claiming


async def claim_next_run(
    factory: SessionFactory, worker_id: str, settings: Settings
) -> uuid.UUID | None:
    # SKIP LOCKED keeps concurrent workers off each other's candidates on PostgreSQL; the
    # conditional UPDATE is what actually guarantees a run is claimed at most once.
    async with factory() as db:
        candidate = await db.scalar(
            select(Run.id)
            .where(Run.status == RunStatus.queued, Run.cancel_requested.is_(False))
            .order_by(Run.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if candidate is None:
            return None
        now = utcnow()
        claimed = await db.execute(
            update(Run)
            .where(Run.id == candidate, Run.status == RunStatus.queued)
            .values(
                status=RunStatus.running,
                worker_id=worker_id,
                attempts=Run.attempts + 1,
                lease_expires_at=now + timedelta(seconds=settings.run_lease_seconds),
                started_at=func.coalesce(Run.started_at, now),
            )
            .returning(Run.id)
        )
        run_id = claimed.scalar()
        await db.commit()
        return run_id


async def recover_stale_runs(factory: SessionFactory, settings: Settings) -> int:
    """Requeue (or fail, after too many attempts) runs whose worker stopped renewing."""
    async with factory() as db:
        stale = (
            await db.scalars(
                select(Run)
                .where(Run.status == RunStatus.running, Run.lease_expires_at < utcnow())
                .with_for_update(skip_locked=True)
            )
        ).all()
        for run in stale:
            message = await db.get(Message, run.output_message_id)
            if run.cancel_requested:
                await finish_cancelled(db, run)
                continue
            if run.attempts < settings.run_max_attempts:
                log.warning("Requeueing run %s after lost lease (attempt %d)", run.id, run.attempts)
                run.status = RunStatus.queued
                run.worker_id = None
                run.lease_expires_at = None
                if message is not None:
                    await db.execute(
                        delete(MessagePart).where(MessagePart.message_id == message.id)
                    )
                    message.status = MessageStatus.pending
                await append_event(
                    db,
                    run.id,
                    RunEventType.run_progress,
                    {
                        "stage": "restarted",
                        "message_id": str(run.output_message_id),
                        "attempt": run.attempts,
                    },
                )
            else:
                log.error("Run %s failed: worker lost %d times", run.id, run.attempts)
                await _mark_failed(
                    db,
                    run,
                    message,
                    "worker_lost",
                    "The worker stopped while generating",
                    retryable=True,
                )
        await db.commit()
        return len(stale)


# ---------------------------------------------------------------------- execution


@dataclass
class _StreamState:
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    pending_text: str = ""
    pending_reasoning: str = ""
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    cancel_requested: bool = False
    lease_lost: bool = False


async def _renew_lease(
    factory: SessionFactory,
    run_id: uuid.UUID,
    worker_id: str,
    settings: Settings,
    state: _StreamState,
    *,
    flush_deltas: bool,
) -> None:
    """Persist pending deltas and renew the lease in one transaction; update control flags."""
    async with factory() as db:
        renewed = await db.execute(
            update(Run)
            .where(Run.id == run_id, Run.worker_id == worker_id, Run.status == RunStatus.running)
            .values(lease_expires_at=utcnow() + timedelta(seconds=settings.run_lease_seconds))
            .returning(Run.cancel_requested, Run.output_message_id)
        )
        row = renewed.first()
        if row is None:
            state.lease_lost = True
            await db.rollback()
            return
        cancel_requested, message_id = row
        if flush_deltas and (state.pending_text or state.pending_reasoning):
            data: dict[str, Any] = {"message_id": str(message_id)}
            if state.pending_text:
                data["text"] = state.pending_text
            if state.pending_reasoning:
                data["reasoning"] = state.pending_reasoning
            await append_event(db, run_id, RunEventType.message_delta, data)
            state.pending_text = ""
            state.pending_reasoning = ""
        await db.commit()
        state.cancel_requested = bool(cancel_requested)


async def _consume(
    factory: SessionFactory,
    gateway: ModelGateway,
    request: ChatRequest,
    run_id: uuid.UUID,
    worker_id: str,
    settings: Settings,
    state: _StreamState,
) -> None:
    flush_interval = settings.run_delta_flush_ms / 1000
    last_flush = time.monotonic()
    stream = gateway.stream_chat(request)
    try:
        async for chunk in stream:
            if chunk.text:
                state.text.append(chunk.text)
                state.pending_text += chunk.text
            if chunk.reasoning:
                state.reasoning.append(chunk.reasoning)
                state.pending_reasoning += chunk.reasoning
            if chunk.usage:
                state.usage = chunk.usage
            if chunk.finish_reason:
                state.finish_reason = chunk.finish_reason
            if time.monotonic() - last_flush >= flush_interval:
                await _renew_lease(factory, run_id, worker_id, settings, state, flush_deltas=True)
                last_flush = time.monotonic()
                if state.cancel_requested or state.lease_lost:
                    return
    finally:
        # Closing the stream closes the HTTP response, which makes llama-server stop generating.
        await stream.aclose()


async def _heartbeat(
    factory: SessionFactory,
    run_id: uuid.UUID,
    worker_id: str,
    settings: Settings,
    state: _StreamState,
    consumer: asyncio.Task,
) -> None:
    interval = min(settings.run_lease_seconds / 3, HEARTBEAT_MAX_INTERVAL_SECONDS)
    while not consumer.done():
        await asyncio.sleep(interval)
        if consumer.done():
            return
        await _renew_lease(factory, run_id, worker_id, settings, state, flush_deltas=False)
        if state.cancel_requested or state.lease_lost:
            consumer.cancel()
            return


async def execute_run(
    run_id: uuid.UUID,
    *,
    factory: SessionFactory,
    gateway: ModelGateway,
    settings: Settings,
    worker_id: str,
) -> None:
    try:
        prepared = await _prepare(run_id, factory, gateway, settings, worker_id)
    except Exception:
        log.exception("Failed to prepare run %s", run_id)
        await _finish_failed(factory, run_id, worker_id, "internal_error", "Internal error")
        return
    if prepared is None:
        return

    state = _StreamState()
    consumer = asyncio.create_task(
        _consume(factory, gateway, prepared, run_id, worker_id, settings, state)
    )
    heartbeat = asyncio.create_task(
        _heartbeat(factory, run_id, worker_id, settings, state, consumer)
    )
    try:
        await consumer
    except asyncio.CancelledError:
        if not (state.cancel_requested or state.lease_lost):
            raise  # worker shutdown: leave the run for lease recovery
    except ModelGatewayError as exc:
        await _finish(factory, run_id, worker_id, state, error=exc)
        return
    except Exception:
        log.exception("Run %s crashed while streaming", run_id)
        await _finish(
            factory,
            run_id,
            worker_id,
            state,
            error=ModelGatewayError("internal_error", "Internal error"),
        )
        return
    finally:
        heartbeat.cancel()
        consumer.cancel()

    if state.lease_lost:
        log.warning("Run %s: lease lost, abandoning without writing results", run_id)
        return
    await _finish(factory, run_id, worker_id, state)


async def _prepare(
    run_id: uuid.UUID,
    factory: SessionFactory,
    gateway: ModelGateway,
    settings: Settings,
    worker_id: str,
) -> ChatRequest | None:
    async with factory() as db:
        run = await db.get(Run, run_id)
        if run is None or run.status != RunStatus.running or run.worker_id != worker_id:
            return None
        conversation = await db.get(Conversation, run.conversation_id)
        output = await db.get(Message, run.output_message_id)
        assert conversation is not None and output is not None

        path = await message_ancestry(db, conversation.id, run.trigger_message_id)
        parts = await load_parts(db, path)
        history = [
            HistoryItem(message.role.value, message_text(parts[message.id]))
            for message in path
            if message.role == MessageRole.user
            or (message.role == MessageRole.assistant and message.status == MessageStatus.complete)
        ]
        system_prompt = settings.default_system_prompt
        if conversation.project_id:
            project = await db.get(Project, conversation.project_id)
            if project and project.instructions:
                system_prompt = f"{system_prompt}\n\n{project.instructions}".strip()

        max_tokens = run.params.get("max_tokens") or settings.model_max_output_tokens
        try:
            model = await gateway.find_model(run.model_id)
            context_tokens = (model.context_tokens if model else None) or (
                settings.model_default_context_tokens
            )
            budget = context_budget(
                context_tokens, max_tokens, settings.context_safety_margin_tokens
            )
            built = build_context(system_prompt, history, budget)
        except (ModelGatewayError, AppError) as exc:
            await _mark_failed(
                db, run, output, exc.code, str(exc), retryable=getattr(exc, "retryable", False)
            )
            await db.commit()
            return None

        output.status = MessageStatus.streaming
        await append_event(
            db,
            run.id,
            RunEventType.run_started,
            {
                "message_id": str(output.id),
                "model_id": run.model_id,
                "attempt": run.attempts,
                "estimated_prompt_tokens": built.estimated_tokens,
                "dropped_messages": built.dropped_messages,
            },
        )
        await db.commit()

        return ChatRequest(
            model=run.model_id,
            messages=built.messages,
            temperature=run.params.get("temperature"),
            top_p=run.params.get("top_p"),
            max_tokens=max_tokens,
        )


async def _finish(
    factory: SessionFactory,
    run_id: uuid.UUID,
    worker_id: str,
    state: _StreamState,
    *,
    error: ModelGatewayError | None = None,
) -> None:
    async with factory() as db:
        run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or run.worker_id != worker_id or run.status != RunStatus.running:
            return  # another worker took over, or it was finished elsewhere
        message = await db.get(Message, run.output_message_id)
        assert message is not None

        # Final deltas go out before the terminal event so replaying clients see all text.
        if state.pending_text or state.pending_reasoning:
            data: dict[str, Any] = {"message_id": str(message.id)}
            if state.pending_text:
                data["text"] = state.pending_text
            if state.pending_reasoning:
                data["reasoning"] = state.pending_reasoning
            await append_event(db, run.id, RunEventType.message_delta, data)

        await db.execute(delete(MessagePart).where(MessagePart.message_id == message.id))
        position = 0
        if state.reasoning:
            db.add(
                MessagePart(
                    message_id=message.id,
                    revision=message.current_revision,
                    position=position,
                    type="reasoning",
                    text="".join(state.reasoning),
                )
            )
            position += 1
        db.add(
            MessagePart(
                message_id=message.id,
                revision=message.current_revision,
                position=position,
                type="text",
                text="".join(state.text),
            )
        )

        run.usage = state.usage
        run.lease_expires_at = None
        if error is not None:
            await _mark_failed(db, run, message, error.code, str(error), retryable=error.retryable)
        elif state.cancel_requested or run.cancel_requested:
            await finish_cancelled(db, run)
        else:
            run.status = RunStatus.completed
            run.finished_at = utcnow()
            message.status = MessageStatus.complete
            await append_event(
                db,
                run.id,
                RunEventType.run_completed,
                {
                    "message_id": str(message.id),
                    "usage": state.usage,
                    "finish_reason": state.finish_reason,
                },
            )
        if error is None and not (state.cancel_requested or run.cancel_requested):
            record_sync(
                db,
                run.owner_id,
                "message",
                message.id,
                payload={"conversation_id": str(run.conversation_id)},
            )
        await db.commit()


async def _finish_failed(
    factory: SessionFactory, run_id: uuid.UUID, worker_id: str, code: str, message: str
) -> None:
    async with factory() as db:
        run = await db.get(Run, run_id)
        if run is None or run.worker_id != worker_id or run.status != RunStatus.running:
            return
        output = await db.get(Message, run.output_message_id)
        await _mark_failed(db, run, output, code, message, retryable=False)
        await db.commit()


async def _mark_failed(
    db, run: Run, message: Message | None, code: str, error: str, *, retryable: bool
) -> None:
    run.status = RunStatus.failed
    run.error_code = code
    run.error_message = error[:2000]
    run.finished_at = utcnow()
    run.lease_expires_at = None
    if message is not None:
        message.status = MessageStatus.failed
        record_sync(
            db,
            run.owner_id,
            "message",
            message.id,
            payload={"conversation_id": str(run.conversation_id)},
        )
    await append_event(
        db,
        run.id,
        RunEventType.run_failed,
        {
            "message_id": str(run.output_message_id),
            "code": code,
            "message": error[:500],
            "retryable": retryable,
        },
    )
