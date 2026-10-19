"""Message submission: persist the user's message and enqueue a generation run atomically."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError
from app.models import (
    Branch,
    Message,
    MessagePart,
    MessageRevision,
    MessageRole,
    MessageStatus,
    Run,
    RunStatus,
    User,
    UserSettings,
)
from app.schemas.conversations import MessageSubmit, MessageSubmitResponse
from app.services.conversations import (
    active_run,
    check_version,
    get_owned_conversation,
    message_out,
    record_sync,
    run_out,
    touch,
)

TITLE_LENGTH = 80


async def _resolve_model(
    db: AsyncSession,
    user: User,
    requested: str | None,
    conversation_override: str | None,
    settings: Settings,
) -> str:
    if requested:
        return requested
    if conversation_override:
        return conversation_override
    user_default = await db.scalar(
        select(UserSettings.default_model_id).where(UserSettings.user_id == user.id)
    )
    return user_default or settings.model_default_id


async def _existing_submission(
    db: AsyncSession, user: User, conversation_id: uuid.UUID, client_message_id: str
) -> MessageSubmitResponse | None:
    message = await db.scalar(
        select(Message).where(
            Message.owner_id == user.id, Message.client_message_id == client_message_id
        )
    )
    if message is None:
        return None
    if message.conversation_id != conversation_id:
        raise AppError(
            409,
            "client_message_id_reused",
            "client_message_id already used in another conversation",
        )
    run = await db.scalar(select(Run).where(Run.trigger_message_id == message.id))
    assert run is not None and run.output_message_id is not None
    assistant = await db.get(Message, run.output_message_id)
    parts = (
        await db.scalars(
            select(MessagePart)
            .where(
                MessagePart.message_id == message.id,
                MessagePart.revision == message.current_revision,
            )
            .order_by(MessagePart.position)
        )
    ).all()
    conversation = await get_owned_conversation(db, user.id, conversation_id)
    return MessageSubmitResponse(
        message=message_out(message, list(parts)),
        assistant_message=message_out(assistant, []),
        run=run_out(run),
        conversation_version=conversation.version,
        duplicate=True,
    )


async def submit_message(
    db: AsyncSession,
    user: User,
    conversation_id: uuid.UUID,
    body: MessageSubmit,
    settings: Settings,
) -> MessageSubmitResponse:
    # Idempotency: a retried submission (e.g. from an offline queue) returns the original.
    existing = await _existing_submission(db, user, conversation_id, body.client_message_id)
    if existing is not None:
        return existing

    conversation = await get_owned_conversation(db, user.id, conversation_id, for_update=True)
    check_version(conversation, body.expected_version)
    if conversation.archived_at is not None:
        raise AppError(409, "conversation_archived", "Unarchive the conversation first")

    branch = await db.get(Branch, conversation.active_branch_id)
    if branch is None:
        raise AppError(500, "branch_missing", "Conversation has no active branch")
    if body.parent_message_id is not None and body.parent_message_id != branch.head_message_id:
        # Forking from an earlier message is the branching feature (phase 3).
        raise AppError(
            409, "parent_not_head", "Messages can only be appended to the end of the active branch"
        )
    if await active_run(db, conversation.id) is not None:
        raise AppError(409, "run_in_progress", "Wait for the current response or cancel it")

    model_id = await _resolve_model(db, user, body.model, conversation.model_override, settings)

    user_message = Message(
        id=uuid.uuid4(),
        owner_id=user.id,
        conversation_id=conversation.id,
        branch_id=branch.id,
        parent_id=branch.head_message_id,
        role=MessageRole.user,
        status=MessageStatus.complete,
        client_message_id=body.client_message_id,
        current_revision=1,
    )
    assistant_message = Message(
        id=uuid.uuid4(),
        owner_id=user.id,
        conversation_id=conversation.id,
        branch_id=branch.id,
        parent_id=user_message.id,
        role=MessageRole.assistant,
        status=MessageStatus.pending,
        current_revision=1,
        model_id=model_id,
    )
    user_part = MessagePart(
        message_id=user_message.id, revision=1, position=0, type="text", text=body.content
    )
    run = Run(
        id=uuid.uuid4(),
        owner_id=user.id,
        conversation_id=conversation.id,
        trigger_message_id=user_message.id,
        output_message_id=assistant_message.id,
        status=RunStatus.queued,
        model_id=model_id,
        params=body.params.model_dump(exclude_none=True),
    )
    db.add(user_message)
    await db.flush()
    db.add(assistant_message)
    await db.flush()
    db.add_all(
        [
            MessageRevision(message_id=user_message.id, revision=1, reason="created"),
            MessageRevision(message_id=assistant_message.id, revision=1, reason="created"),
            user_part,
            run,
        ]
    )
    branch.head_message_id = assistant_message.id
    if not conversation.title:
        conversation.title = " ".join(body.content.split())[:TITLE_LENGTH]
    touch(db, conversation)
    record_sync(db, user.id, "message", user_message.id)
    record_sync(db, user.id, "message", assistant_message.id)

    try:
        await db.commit()
    except IntegrityError:
        # A concurrent duplicate of the same client_message_id won the race.
        await db.rollback()
        existing = await _existing_submission(db, user, conversation_id, body.client_message_id)
        if existing is None:
            raise
        return existing

    return MessageSubmitResponse(
        message=message_out(user_message, [user_part]),
        assistant_message=message_out(assistant_message, []),
        run=run_out(run),
        conversation_version=conversation.version,
    )
