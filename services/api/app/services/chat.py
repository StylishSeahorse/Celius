"""Writing to a conversation: submit, edit, regenerate and branch selection.

Each operation persists its messages and enqueues the generation run in one transaction,
so a crash can never leave a saved message without a recoverable run. Branches never
overwrite history: an edit or a regeneration forks a new branch from the shared parent,
and the conversation's active branch decides which path is shown.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError, not_found
from app.models import (
    Branch,
    Conversation,
    FileLink,
    FileStatus,
    Message,
    MessagePart,
    MessageRevision,
    MessageRole,
    MessageStatus,
    Run,
    RunStatus,
    StoredFile,
    User,
    UserSettings,
)
from app.schemas.conversations import (
    GenerationParams,
    MessageEdit,
    MessageSubmit,
    MessageSubmitResponse,
    Regenerate,
    RegenerateResponse,
)
from app.services import limits
from app.services.conversations import (
    active_run,
    check_version,
    get_owned_conversation,
    message_out,
    run_out,
    touch,
)
from app.services.sync import record_sync

TITLE_LENGTH = 80


async def _resolve_model(
    db: AsyncSession,
    user: User,
    requested: str | None,
    conversation_override: str | None,
    settings: Settings,
) -> str:
    if requested:
        if not limits.model_allowed(user, requested):
            raise AppError(403, "model_not_allowed", "That model isn't available to you")
        return requested
    user_default = await db.scalar(
        select(UserSettings.default_model_id).where(UserSettings.user_id == user.id)
    )
    for candidate in (conversation_override, user_default, settings.model_default_id):
        if candidate and limits.model_allowed(user, candidate):
            return candidate
    if user.allowed_models:
        return user.allowed_models[0]
    raise AppError(403, "model_not_allowed", "No model is available to you; ask an administrator")


async def _writable_conversation(
    db: AsyncSession, user: User, conversation_id: uuid.UUID, expected_version: int | None = None
) -> Conversation:
    conversation = await get_owned_conversation(db, user.id, conversation_id, for_update=True)
    check_version(conversation, expected_version)
    if conversation.archived_at is not None:
        raise AppError(409, "conversation_archived", "Unarchive the conversation first")
    if await active_run(db, conversation.id) is not None:
        raise AppError(409, "run_in_progress", "Wait for the current response or cancel it")
    return conversation


async def _parts_of(db: AsyncSession, message: Message) -> list[MessagePart]:
    return list(
        (
            await db.scalars(
                select(MessagePart)
                .where(
                    MessagePart.message_id == message.id,
                    MessagePart.revision == message.current_revision,
                )
                .order_by(MessagePart.position)
            )
        ).all()
    )


async def _attachments(db: AsyncSession, user: User, file_ids: list[uuid.UUID]) -> list[StoredFile]:
    """Validate attachments: the caller's own, not deleted, finished processing."""
    if not file_ids:
        return []
    if len(set(file_ids)) != len(file_ids):
        raise AppError(422, "duplicate_attachment", "A file is attached more than once")
    rows = (
        await db.scalars(
            select(StoredFile).where(
                StoredFile.id.in_(file_ids),
                StoredFile.owner_id == user.id,
                StoredFile.deleted_at.is_(None),
            )
        )
    ).all()
    by_id = {f.id: f for f in rows}
    if len(by_id) != len(file_ids):
        raise not_found("Attachment")
    files = [by_id[i] for i in file_ids]
    for file in files:
        if file.status == FileStatus.processing:
            raise AppError(409, "attachment_not_ready", f"{file.filename} is still being processed")
    return files


async def _add_user_message(
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    branch: Branch,
    parent_id: uuid.UUID | None,
    content: str,
    client_message_id: str,
    files: list[StoredFile] = (),
    reason: str = "created",
) -> tuple[Message, list[MessagePart]]:
    message = Message(
        id=uuid.uuid4(),
        owner_id=user.id,
        conversation_id=conversation.id,
        branch_id=branch.id,
        parent_id=parent_id,
        role=MessageRole.user,
        status=MessageStatus.complete,
        client_message_id=client_message_id,
        current_revision=1,
    )
    parts = [MessagePart(message_id=message.id, revision=1, position=0, type="text", text=content)]
    for position, file in enumerate(files, start=1):
        parts.append(
            MessagePart(
                message_id=message.id,
                revision=1,
                position=position,
                type="file",
                data={
                    "file_id": str(file.id),
                    "filename": file.filename,
                    "relative_path": file.relative_path,
                    "media_type": file.media_type,
                    "kind": file.kind.value,
                    "size_bytes": file.size_bytes,
                    "has_thumbnail": file.has_thumbnail,
                },
            )
        )
    db.add(message)
    await db.flush()
    db.add_all([MessageRevision(message_id=message.id, revision=1, reason=reason), *parts])
    db.add_all(
        FileLink(message_id=message.id, file_id=file.id, position=position)
        for position, file in enumerate(files)
    )
    record_sync(
        db, user.id, "message", message.id, payload={"conversation_id": str(conversation.id)}
    )
    return message, parts


async def _enqueue_reply(
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    branch: Branch,
    trigger: Message,
    model_id: str,
    params: GenerationParams,
    client_message_id: str | None = None,
    reason: str = "created",
) -> tuple[Message, Run]:
    """Create the pending assistant message under `trigger` and queue its run."""
    assistant = Message(
        id=uuid.uuid4(),
        owner_id=user.id,
        conversation_id=conversation.id,
        branch_id=branch.id,
        parent_id=trigger.id,
        role=MessageRole.assistant,
        status=MessageStatus.pending,
        client_message_id=client_message_id,
        current_revision=1,
        model_id=model_id,
    )
    run = Run(
        id=uuid.uuid4(),
        owner_id=user.id,
        conversation_id=conversation.id,
        trigger_message_id=trigger.id,
        output_message_id=assistant.id,
        status=RunStatus.queued,
        model_id=model_id,
        params=_run_params(conversation, params),
    )
    db.add(assistant)
    await db.flush()
    db.add_all([MessageRevision(message_id=assistant.id, revision=1, reason=reason), run])
    branch.head_message_id = assistant.id
    record_sync(
        db, user.id, "message", assistant.id, payload={"conversation_id": str(conversation.id)}
    )
    return assistant, run


def _run_params(conversation: Conversation, params: GenerationParams) -> dict:
    resolved = params.model_dump(exclude_none=True)
    if "reasoning" not in resolved and conversation.reasoning is not None:
        resolved["reasoning"] = conversation.reasoning
    return resolved


async def _new_branch(
    db: AsyncSession, conversation: Conversation, fork_message_id: uuid.UUID | None
) -> Branch:
    branch = Branch(
        id=uuid.uuid4(), conversation_id=conversation.id, fork_message_id=fork_message_id
    )
    db.add(branch)
    await db.flush()
    conversation.active_branch_id = branch.id
    return branch


async def _owned_message(db: AsyncSession, user: User, message_id: uuid.UUID) -> Message:
    message = await db.scalar(
        select(Message).where(Message.id == message_id, Message.owner_id == user.id)
    )
    if message is None:
        raise not_found("Message")
    return message


# ---------------------------------------------------------------------- idempotency


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
    if message.conversation_id != conversation_id or message.role != MessageRole.user:
        raise AppError(
            409, "client_message_id_reused", "client_message_id was already used elsewhere"
        )
    run = await db.scalar(select(Run).where(Run.trigger_message_id == message.id))
    assert run is not None and run.output_message_id is not None
    assistant = await db.get(Message, run.output_message_id)
    conversation = await get_owned_conversation(db, user.id, conversation_id)
    return MessageSubmitResponse(
        message=message_out(message, await _parts_of(db, message)),
        assistant_message=message_out(assistant, []),
        run=run_out(run),
        conversation_version=conversation.version,
        duplicate=True,
    )


async def _commit_or_existing(db: AsyncSession, fetch_existing):
    """Commit; if a concurrent duplicate of the same client id won the race, return it."""
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await fetch_existing()
        if existing is None:
            raise
        return existing
    return None


# ---------------------------------------------------------------------- operations


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

    conversation = await _writable_conversation(db, user, conversation_id, body.expected_version)
    branch = await db.get(Branch, conversation.active_branch_id)
    if branch is None:
        raise AppError(500, "branch_missing", "Conversation has no active branch")
    if body.parent_message_id is not None and body.parent_message_id != branch.head_message_id:
        # The client composed against an older view (e.g. another device moved the branch).
        raise AppError(
            409,
            "parent_not_head",
            "The conversation changed since this message was written",
            head_message_id=str(branch.head_message_id) if branch.head_message_id else None,
        )

    await limits.check_daily_messages(db, user)
    model_id = await _resolve_model(db, user, body.model, conversation.model_override, settings)
    files = await _attachments(db, user, body.attachment_ids)
    message, parts = await _add_user_message(
        db,
        user,
        conversation,
        branch,
        branch.head_message_id,
        body.content,
        body.client_message_id,
        files,
    )
    assistant, run = await _enqueue_reply(
        db, user, conversation, branch, message, model_id, body.params
    )
    if not conversation.title:
        title = " ".join(body.content.split()) or (files[0].filename if files else "")
        conversation.title = title[:TITLE_LENGTH] or None
    touch(db, conversation)

    duplicate = await _commit_or_existing(
        db, lambda: _existing_submission(db, user, conversation_id, body.client_message_id)
    )
    if duplicate is not None:
        return duplicate
    return MessageSubmitResponse(
        message=message_out(message, parts),
        assistant_message=message_out(assistant, []),
        run=run_out(run),
        conversation_version=conversation.version,
    )


async def edit_message(
    db: AsyncSession, user: User, message_id: uuid.UUID, body: MessageEdit, settings: Settings
) -> MessageSubmitResponse:
    """Fork a new branch with an edited copy of a user message and a fresh reply."""
    original = await _owned_message(db, user, message_id)
    existing = await _existing_submission(
        db, user, original.conversation_id, body.client_message_id
    )
    if existing is not None:
        return existing
    if original.role != MessageRole.user:
        raise AppError(422, "not_a_user_message", "Only your own messages can be edited")

    conversation = await _writable_conversation(db, user, original.conversation_id)
    branch = await _new_branch(db, conversation, original.parent_id)
    await limits.check_daily_messages(db, user)
    model_id = await _resolve_model(db, user, body.model, conversation.model_override, settings)
    files = await _attachments(db, user, body.attachment_ids)
    message, parts = await _add_user_message(
        db,
        user,
        conversation,
        branch,
        original.parent_id,
        body.content,
        body.client_message_id,
        files,
        reason="edited",
    )
    assistant, run = await _enqueue_reply(
        db, user, conversation, branch, message, model_id, body.params
    )
    touch(db, conversation)

    duplicate = await _commit_or_existing(
        db,
        lambda: _existing_submission(db, user, original.conversation_id, body.client_message_id),
    )
    if duplicate is not None:
        return duplicate
    return MessageSubmitResponse(
        message=message_out(message, parts),
        assistant_message=message_out(assistant, []),
        run=run_out(run),
        conversation_version=conversation.version,
    )


async def _existing_regeneration(
    db: AsyncSession, user: User, conversation_id: uuid.UUID, client_request_id: str | None
) -> RegenerateResponse | None:
    if client_request_id is None:
        return None
    assistant = await db.scalar(
        select(Message).where(
            Message.owner_id == user.id, Message.client_message_id == client_request_id
        )
    )
    if assistant is None:
        return None
    if assistant.conversation_id != conversation_id or assistant.role != MessageRole.assistant:
        raise AppError(
            409, "client_message_id_reused", "client_request_id was already used elsewhere"
        )
    run = await db.scalar(select(Run).where(Run.output_message_id == assistant.id))
    conversation = await get_owned_conversation(db, user.id, conversation_id)
    return RegenerateResponse(
        assistant_message=message_out(assistant, await _parts_of(db, assistant)),
        run=run_out(run),
        conversation_version=conversation.version,
        duplicate=True,
    )


async def regenerate(
    db: AsyncSession, user: User, message_id: uuid.UUID, body: Regenerate, settings: Settings
) -> RegenerateResponse:
    """Fork a new branch with an alternative reply to the same user message."""
    original = await _owned_message(db, user, message_id)
    existing = await _existing_regeneration(
        db, user, original.conversation_id, body.client_request_id
    )
    if existing is not None:
        return existing
    if original.role != MessageRole.assistant or original.parent_id is None:
        raise AppError(422, "not_an_assistant_message", "Only replies can be regenerated")

    conversation = await _writable_conversation(db, user, original.conversation_id)
    trigger = await db.get(Message, original.parent_id)
    assert trigger is not None
    branch = await _new_branch(db, conversation, trigger.id)
    await limits.check_daily_messages(db, user)
    model_id = await _resolve_model(db, user, body.model, conversation.model_override, settings)
    assistant, run = await _enqueue_reply(
        db,
        user,
        conversation,
        branch,
        trigger,
        model_id,
        body.params,
        client_message_id=body.client_request_id,
        reason="regenerated",
    )
    touch(db, conversation)

    duplicate = await _commit_or_existing(
        db,
        lambda: _existing_regeneration(db, user, original.conversation_id, body.client_request_id),
    )
    if duplicate is not None:
        return duplicate
    return RegenerateResponse(
        assistant_message=message_out(assistant, []),
        run=run_out(run),
        conversation_version=conversation.version,
    )


async def select_message(db: AsyncSession, user: User, message_id: uuid.UUID) -> Conversation:
    """Show the branch containing `message_id`, following its most recent continuation.

    Used for "‹ 2 / 3 ›" navigation between edits and regenerated replies.
    """
    target = await _owned_message(db, user, message_id)
    conversation = await get_owned_conversation(
        db, user.id, target.conversation_id, for_update=True
    )
    if await active_run(db, conversation.id) is not None:
        raise AppError(409, "run_in_progress", "Wait for the current response or cancel it")

    rows = (
        await db.scalars(select(Message).where(Message.conversation_id == conversation.id))
    ).all()
    children: dict[uuid.UUID, list[Message]] = {}
    for message in rows:
        if message.parent_id is not None:
            children.setdefault(message.parent_id, []).append(message)
    leaf = target
    while children.get(leaf.id):
        leaf = max(children[leaf.id], key=lambda m: (m.created_at, str(m.id)))

    branch = await db.scalar(
        select(Branch).where(
            Branch.conversation_id == conversation.id, Branch.head_message_id == leaf.id
        )
    )
    if branch is None:  # not expected: every leaf is some branch's head
        branch = Branch(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            head_message_id=leaf.id,
            fork_message_id=target.parent_id,
        )
        db.add(branch)
        await db.flush()
    if conversation.active_branch_id != branch.id:
        conversation.active_branch_id = branch.id
        touch(db, conversation)
    await db.commit()
    return conversation
