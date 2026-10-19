from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.db import get_db
from app.schemas.sync import SyncAck, SyncChange, SyncChanges, SyncCursor
from app.services import sync as svc

router = APIRouter(prefix="/sync", tags=["sync"])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("/cursor", response_model=SyncCursor)
async def get_cursor(auth: CurrentAuth, db: DB) -> SyncCursor:
    """The latest cursor. A client doing a full load starts following changes from here."""
    return SyncCursor(cursor=await svc.current_cursor(db, auth.user.id))


@router.get("/changes", response_model=SyncChanges)
async def get_changes(
    auth: CurrentAuth,
    db: DB,
    cursor: Annotated[int, Query(ge=0)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> SyncChanges:
    events, has_more = await svc.changes_since(db, auth.user.id, cursor, limit)
    return SyncChanges(
        changes=[
            SyncChange(
                cursor=e.id,
                entity_type=e.entity_type,
                entity_id=e.entity_id,
                operation=e.operation,
                payload=e.payload,
                created_at=e.created_at,
            )
            for e in events
        ],
        cursor=events[-1].id if events else cursor,
        has_more=has_more,
    )


@router.post("/ack", status_code=status.HTTP_204_NO_CONTENT)
async def ack(body: SyncAck, auth: CurrentAuth, db: DB) -> None:
    await svc.acknowledge(db, auth.user.id, body.device_id, body.cursor)
    await db.commit()
