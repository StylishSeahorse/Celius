import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.models import MessageRole
from app.schemas.conversations import (
    ConversationCreate,
    ConversationDetail,
    ConversationList,
    ConversationUpdate,
    MessageSubmit,
    MessageSubmitResponse,
)
from app.services import conversations as svc
from app.services.chat import submit_message
from app.services.tools.calls import calls_for_messages

router = APIRouter(prefix="/conversations", tags=["conversations"])

DB = Annotated[AsyncSession, Depends(get_db)]


async def conversation_detail(db: AsyncSession, conversation) -> ConversationDetail:
    path, siblings = await svc.path_with_siblings(db, conversation)
    parts = await svc.load_parts(db, path)
    run = await svc.active_run(db, conversation.id)
    calls = await calls_for_messages(db, [m.id for m in path if m.role == MessageRole.assistant])
    messages = []
    for m in path:
        out = svc.message_out(m, parts[m.id], siblings[m.id])
        out.tool_calls = calls.get(m.id, [])
        messages.append(out)
    return ConversationDetail(
        **svc.conversation_summary(conversation).model_dump(),
        messages=messages,
        active_run=svc.run_out(run) if run else None,
    )


@router.get("", response_model=ConversationList)
async def list_conversations(
    auth: CurrentAuth,
    db: DB,
    q: Annotated[str | None, Query(max_length=200)] = None,
    project_id: uuid.UUID | None = None,
    archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> ConversationList:
    rows, next_cursor = await svc.list_conversations(
        db,
        auth.user.id,
        query=q,
        project_id=project_id,
        archived=archived,
        limit=limit,
        cursor=cursor,
    )
    return ConversationList(
        items=[svc.conversation_summary(c) for c in rows], next_cursor=next_cursor
    )


@router.post(
    "",
    response_model=ConversationDetail,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "A conversation with this client-chosen id already exists"}},
)
async def create_conversation(
    body: ConversationCreate, response: Response, auth: CurrentAuth, db: DB
) -> ConversationDetail:
    try:
        conversation, created = await svc.create_conversation(db, auth.user.id, body)
        await db.commit()
    except IntegrityError:
        # A concurrent retry with the same client id won the race; return that one.
        await db.rollback()
        if body.id is None:
            raise
        conversation, created = await svc.create_conversation(db, auth.user.id, body)
    if not created:
        response.status_code = status.HTTP_200_OK
    return await conversation_detail(db, conversation)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, auth: CurrentAuth, db: DB
) -> ConversationDetail:
    conversation = await svc.get_owned_conversation(db, auth.user.id, conversation_id)
    return await conversation_detail(db, conversation)


@router.patch("/{conversation_id}", response_model=ConversationDetail)
async def update_conversation(
    conversation_id: uuid.UUID, body: ConversationUpdate, auth: CurrentAuth, db: DB
) -> ConversationDetail:
    conversation = await svc.get_owned_conversation(
        db, auth.user.id, conversation_id, for_update=True
    )
    await svc.update_conversation(db, conversation, body)
    await db.commit()
    return await conversation_detail(db, conversation)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    conversation = await svc.get_owned_conversation(
        db, auth.user.id, conversation_id, for_update=True
    )
    await svc.delete_conversation(db, conversation)
    await db.commit()


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Duplicate submission; the original is returned"}},
)
async def post_message(
    conversation_id: uuid.UUID,
    body: MessageSubmit,
    response: Response,
    auth: CurrentAuth,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
) -> MessageSubmitResponse:
    result = await submit_message(db, auth.user, conversation_id, body, settings)
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return result
