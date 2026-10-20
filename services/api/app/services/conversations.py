"""Conversation persistence: CRUD, branch history and serialization.

Every durable change bumps `Conversation.version` and records a `SyncEvent` in the
same transaction, so other devices can catch up from their last cursor.
"""

import base64
import uuid
from datetime import datetime

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import (
    Branch,
    Conversation,
    Message,
    MessagePart,
    Run,
    RunStatus,
)
from app.schemas.conversations import (
    ConversationCreate,
    ConversationSummary,
    ConversationUpdate,
    MessageOut,
    MessagePartOut,
    RunOut,
)
from app.services.access import require_project
from app.services.audit import audit
from app.services.memory.store import forget_conversation, move_chunks
from app.services.runs import request_cancel
from app.services.sync import record_sync
from app.services.tools.policy import validate_profile_key

ACTIVE_RUN_STATUSES = (RunStatus.queued, RunStatus.running, RunStatus.awaiting_approval)


def touch(db: AsyncSession, conversation: Conversation) -> None:
    conversation.version += 1
    conversation.updated_at = utcnow()
    record_sync(
        db,
        conversation.owner_id,
        "conversation",
        conversation.id,
        payload={"version": conversation.version},
    )


def check_version(conversation: Conversation, expected: int | None) -> None:
    if expected is not None and expected != conversation.version:
        raise AppError(
            409,
            "version_conflict",
            "The conversation changed on another device",
            current_version=conversation.version,
            # The server's state, so the client can offer "keep theirs / use mine".
            current=conversation_summary(conversation).model_dump(mode="json"),
        )


# ---------------------------------------------------------------------- queries


async def get_owned_conversation(
    db: AsyncSession, owner_id: uuid.UUID, conversation_id: uuid.UUID, *, for_update: bool = False
) -> Conversation:
    query = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.owner_id == owner_id,
        Conversation.deleted_at.is_(None),
    )
    if for_update:
        query = query.with_for_update()
    conversation = await db.scalar(query)
    if conversation is None:
        raise not_found("Conversation")
    return conversation


async def active_run(db: AsyncSession, conversation_id: uuid.UUID) -> Run | None:
    return await db.scalar(
        select(Run)
        .where(Run.conversation_id == conversation_id, Run.status.in_(ACTIVE_RUN_STATUSES))
        .order_by(Run.created_at.desc())
        .limit(1)
    )


async def branch_path(db: AsyncSession, conversation: Conversation) -> list[Message]:
    """Messages from the root to the head of the active branch, oldest first."""
    branch = await db.get(Branch, conversation.active_branch_id)
    if branch is None or branch.head_message_id is None:
        return []
    return await message_ancestry(db, conversation.id, branch.head_message_id)


async def path_with_siblings(
    db: AsyncSession, conversation: Conversation
) -> tuple[list[Message], dict[uuid.UUID, list[uuid.UUID]]]:
    """The active path plus, for each message on it, its siblings (alternative edits or
    regenerated replies sharing the same parent), oldest first."""
    rows = (
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at, Message.id)
        )
    ).all()
    children: dict[uuid.UUID | None, list[uuid.UUID]] = {}
    for message in rows:
        children.setdefault(message.parent_id, []).append(message.id)
    branch = await db.get(Branch, conversation.active_branch_id)
    by_id = {m.id: m for m in rows}
    path: list[Message] = []
    current = by_id.get(branch.head_message_id) if branch and branch.head_message_id else None
    while current is not None:
        path.append(current)
        current = by_id.get(current.parent_id) if current.parent_id else None
    path.reverse()
    return path, {m.id: children.get(m.parent_id, [m.id]) for m in path}


async def message_ancestry(
    db: AsyncSession, conversation_id: uuid.UUID, head_id: uuid.UUID
) -> list[Message]:
    rows = (
        await db.scalars(select(Message).where(Message.conversation_id == conversation_id))
    ).all()
    by_id = {m.id: m for m in rows}
    path: list[Message] = []
    current = by_id.get(head_id)
    while current is not None:
        path.append(current)
        current = by_id.get(current.parent_id) if current.parent_id else None
    path.reverse()
    return path


async def load_parts(
    db: AsyncSession, messages: list[Message]
) -> dict[uuid.UUID, list[MessagePart]]:
    if not messages:
        return {}
    wanted = {(m.id, m.current_revision) for m in messages}
    parts = (
        await db.scalars(
            select(MessagePart)
            .where(MessagePart.message_id.in_([m.id for m in messages]))
            .order_by(MessagePart.position)
        )
    ).all()
    grouped: dict[uuid.UUID, list[MessagePart]] = {m.id: [] for m in messages}
    for part in parts:
        if (part.message_id, part.revision) in wanted:
            grouped[part.message_id].append(part)
    return grouped


def message_text(parts: list[MessagePart]) -> str:
    return "".join(p.text or "" for p in parts if p.type == "text")


# ---------------------------------------------------------------------- serialization


def conversation_summary(conversation: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        project_id=conversation.project_id,
        model_override=conversation.model_override,
        memory_enabled=conversation.memory_enabled,
        reasoning=conversation.reasoning,
        tool_profile=conversation.tool_profile,
        active_branch_id=conversation.active_branch_id,
        version=conversation.version,
        archived=conversation.archived_at is not None,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def message_out(
    message: Message, parts: list[MessagePart], sibling_ids: list[uuid.UUID] | None = None
) -> MessageOut:
    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        branch_id=message.branch_id,
        parent_id=message.parent_id,
        role=message.role,
        status=message.status,
        client_message_id=message.client_message_id,
        revision=message.current_revision,
        model_id=message.model_id,
        parts=[MessagePartOut(type=p.type, text=p.text, data=p.data) for p in parts],
        sibling_ids=sibling_ids or [message.id],
        created_at=message.created_at,
    )


def run_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        conversation_id=run.conversation_id,
        trigger_message_id=run.trigger_message_id,
        output_message_id=run.output_message_id,
        status=run.status,
        model_id=run.model_id,
        error_code=run.error_code,
        error_message=run.error_message,
        usage=run.usage,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


# ---------------------------------------------------------------------- commands


async def _check_project(db: AsyncSession, owner_id: uuid.UUID, project_id: uuid.UUID) -> None:
    """Chats can go in your projects and in projects shared with you."""
    await require_project(db, owner_id, project_id)


async def create_conversation(
    db: AsyncSession, owner_id: uuid.UUID, data: ConversationCreate
) -> tuple[Conversation, bool]:
    """Create a conversation. A client-chosen `id` makes this idempotent, so a device can
    create conversations offline and retry the request safely. Returns (conversation, created)."""
    if data.id is not None:
        existing = await db.get(Conversation, data.id)
        if existing is not None:
            if existing.owner_id != owner_id or existing.deleted_at is not None:
                raise AppError(409, "id_conflict", "That conversation id is not available")
            return existing, False
    if data.project_id:
        await _check_project(db, owner_id, data.project_id)
    await validate_profile_key(db, owner_id, data.tool_profile)
    conversation = Conversation(
        id=data.id or uuid.uuid4(),
        owner_id=owner_id,
        project_id=data.project_id,
        title=data.title,
        model_override=data.model_override,
        reasoning=data.reasoning,
        tool_profile=data.tool_profile,
        memory_enabled=data.memory_enabled,
        version=1,
    )
    db.add(conversation)
    await db.flush()
    branch = Branch(id=uuid.uuid4(), conversation_id=conversation.id, name="main")
    db.add(branch)
    await db.flush()
    conversation.active_branch_id = branch.id
    record_sync(db, owner_id, "conversation", conversation.id, payload={"version": 1})
    return conversation, True


async def update_conversation(
    db: AsyncSession, conversation: Conversation, data: ConversationUpdate
) -> Conversation:
    check_version(conversation, data.expected_version)
    fields = data.model_dump(exclude_unset=True, exclude={"expected_version", "archived"})
    if fields.get("project_id"):
        await _check_project(db, conversation.owner_id, fields["project_id"])
    if "tool_profile" in fields and fields["tool_profile"] != conversation.tool_profile:
        await validate_profile_key(db, conversation.owner_id, fields["tool_profile"])
        audit(
            db,
            conversation.owner_id,
            "conversation.tool_profile",
            "conversation",
            conversation.id,
            previous=conversation.tool_profile,
            profile=fields["tool_profile"],
        )
    moved = "project_id" in fields and fields["project_id"] != conversation.project_id
    memory_off = fields.get("memory_enabled") is False and conversation.memory_enabled
    for key, value in fields.items():
        setattr(conversation, key, value)
    if memory_off:
        # Not remembered any more: its excerpts go now. Memories learned from it stay
        # until deleted (the UI offers that).
        await forget_conversation(db, conversation, memories_too=False)
    elif moved:
        await move_chunks(db, conversation)
    if data.archived is not None:
        conversation.archived_at = utcnow() if data.archived else None
    touch(db, conversation)
    return conversation


async def delete_conversation(
    db: AsyncSession, conversation: Conversation, *, forget_memories: bool = False
) -> dict[str, int]:
    """Soft delete; active runs are cancelled. Its recall excerpts are removed at once (and
    the memories learned from it, if asked). Purging the rest is a separate retention job."""
    forgotten = await forget_conversation(db, conversation, memories_too=forget_memories)
    now = utcnow()
    conversation.deleted_at = now
    runs = (
        await db.scalars(
            select(Run).where(
                Run.conversation_id == conversation.id, Run.status.in_(ACTIVE_RUN_STATUSES)
            )
        )
    ).all()
    for run in runs:
        await request_cancel(db, run)
    conversation.version += 1
    record_sync(db, conversation.owner_id, "conversation", conversation.id, operation="delete")
    return forgotten


def _encode_cursor(updated_at: datetime, conversation_id: uuid.UUID) -> str:
    raw = f"{updated_at.isoformat()}|{conversation_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, ident = raw.split("|", 1)
        return datetime.fromisoformat(stamp), uuid.UUID(ident)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppError(400, "invalid_cursor", "Invalid cursor") from exc


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_conversations(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    query: str | None = None,
    project_id: uuid.UUID | None = None,
    archived: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Conversation], str | None]:
    stmt = select(Conversation).where(
        Conversation.owner_id == owner_id,
        Conversation.deleted_at.is_(None),
        Conversation.archived_at.is_not(None) if archived else Conversation.archived_at.is_(None),
    )
    if project_id:
        stmt = stmt.where(Conversation.project_id == project_id)
    if query:
        pattern = _like_pattern(query.strip())
        in_messages = exists(
            select(MessagePart.id)
            .join(Message, Message.id == MessagePart.message_id)
            .where(
                Message.conversation_id == Conversation.id,
                MessagePart.revision == Message.current_revision,
                MessagePart.text.ilike(pattern, escape="\\"),
            )
        )
        stmt = stmt.where(or_(Conversation.title.ilike(pattern, escape="\\"), in_messages))
    if cursor:
        stamp, ident = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Conversation.updated_at < stamp,
                and_(Conversation.updated_at == stamp, Conversation.id < ident),
            )
        )
    stmt = stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit + 1)
    rows = list((await db.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1].updated_at, rows[-1].id)
    return rows, next_cursor
