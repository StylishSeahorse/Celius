"""Per-user change feed. Every durable write records a SyncEvent in the same transaction;
clients page through the feed by cursor (the event id) and refetch what changed."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import DeviceSyncState, SyncEvent


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


async def current_cursor(db: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await db.scalar(
            select(func.coalesce(func.max(SyncEvent.id), 0)).where(SyncEvent.user_id == user_id)
        )
        or 0
    )


async def changes_since(
    db: AsyncSession, user_id: uuid.UUID, cursor: int, limit: int
) -> tuple[list[SyncEvent], bool]:
    rows = (
        await db.scalars(
            select(SyncEvent)
            .where(SyncEvent.user_id == user_id, SyncEvent.id > cursor)
            .order_by(SyncEvent.id)
            .limit(limit + 1)
        )
    ).all()
    return list(rows[:limit]), len(rows) > limit


async def acknowledge(db: AsyncSession, user_id: uuid.UUID, device_id: str, cursor: int) -> None:
    """Record how far a device has processed. Never moves a device's cursor backwards."""
    state = await db.get(DeviceSyncState, (user_id, device_id))
    if state is None:
        db.add(DeviceSyncState(user_id=user_id, device_id=device_id, last_ack_cursor=cursor))
    elif cursor > state.last_ack_cursor:
        state.last_ack_cursor = cursor
        state.updated_at = utcnow()


__all__ = ["acknowledge", "changes_since", "current_cursor", "record_sync"]
