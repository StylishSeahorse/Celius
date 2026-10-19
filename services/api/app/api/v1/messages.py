import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import conversation_detail
from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.schemas.conversations import (
    ConversationDetail,
    MessageEdit,
    MessageSubmitResponse,
    Regenerate,
    RegenerateResponse,
)
from app.services import chat

router = APIRouter(prefix="/messages", tags=["messages"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@router.post(
    "/{message_id}/revisions",
    response_model=MessageSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Duplicate request; the original is returned"}},
)
async def edit_message(
    message_id: uuid.UUID,
    body: MessageEdit,
    response: Response,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
) -> MessageSubmitResponse:
    """Edit a user message. The edit becomes a new branch; the original is kept."""
    result = await chat.edit_message(db, auth.user, message_id, body, settings)
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return result


@router.post(
    "/{message_id}/regenerate",
    response_model=RegenerateResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Duplicate request; the original is returned"}},
)
async def regenerate(
    message_id: uuid.UUID,
    body: Regenerate,
    response: Response,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
) -> RegenerateResponse:
    """Generate an alternative reply. It becomes a new branch; the original is kept."""
    result = await chat.regenerate(db, auth.user, message_id, body, settings)
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/{message_id}/select", response_model=ConversationDetail)
async def select_message(message_id: uuid.UUID, auth: CurrentAuth, db: DB) -> ConversationDetail:
    """Switch the conversation to the branch that contains this message."""
    conversation = await chat.select_message(db, auth.user, message_id)
    return await conversation_detail(db, conversation)
