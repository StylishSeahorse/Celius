"""Conversation persistence: CRUD, branch history and serialization.

Every durable change bumps `Conversation.version` and records a `SyncEvent` in the
same transaction, so other devices can catch up from their last cursor.
"""

import base64
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import (
    Branch,
    Conversation,
    Message,
    MessagePart,
    Project,
    Run,
    RunStatus,
    SyncEvent,
)
from app.schemas.conversations import (
    ConversationCreate,
    ConversationSummary,
    ConversationUpdate,
    MessageOut,
    MessagePartOut,
    RunOut,
)
from app.services.runs import request_cancel

ACTIVE_RUN_STATUSES = (RunStatus.queued, RunStatus.running, RunStatus.awaiting_approval)


def record_sync(
    db: AsyncSession,
    user_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    operation: str = "upsert",
    payload: dict[str, Any] | None = None,
) -> None:
    db.add(
        SyncEvent(
            user_id=user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            operation=operation,
            payload=payload,
            created_at=utcnow(),
        )
    )


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
        active_branch_id=conversation.active_branch_id,
        version=conversation.version,
        archived=conversation.archived_at is not None,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def message_out(message: Message, parts: list[MessagePart]) -> MessageOut:
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
    owned = await db.scalar(
        select(Project.id).where(Project.id == project_id, Project.owner_id == owner_id)
    )
    if owned is None:
        raise not_found("Project")


async def create_conversation(
    db: AsyncSession, owner_id: uuid.UUID, data: ConversationCreate
) -> Conversation:
    if data.project_id:
        await _check_project(db, owner_id, data.project_id)
    conversation = Conversation(
        id=uuid.uuid4(),
        owner_id=owner_id,
        project_id=data.project_id,
        title=data.title,
        model_override=data.model_override,
        version=1,
    )
    db.add(conversation)
    await db.flush()
    branch = Branch(id=uuid.uuid4(), conversation_id=conversation.id, name="main")
    db.add(branch)
    await db.flush()
    conversation.active_branch_id = branch.id
    record_sync(db, owner_id, "conversation", conversation.id, payload={"version": 1})
    return conversation


async def update_conversation(
    db: AsyncSession, conversation: Conversation, data: ConversationUpdate
) -> Conversation:
    check_version(conversation, data.expected_version)
    fields = data.model_dump(exclude_unset=True, exclude={"expected_version", "archived"})
    if fields.get("project_id"):
        await _check_project(db, conversation.owner_id, fields["project_id"])
    for key, value in fields.items():
        setattr(conversation, key, value)
    if data.archived is not None:
        conversation.archived_at = utcnow() if data.archived else None
    touch(db, conversation)
    return conversation


async def delete_conversation(db: AsyncSession, conversation: Conversation) -> None:
    """Soft delete; active runs are cancelled. Purging is a separate retention job."""
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
