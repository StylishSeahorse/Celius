"""Per-user storage accounting. Each change is a single conditional UPDATE, so concurrent
uploads can't jointly overshoot the limit (PostgreSQL row lock / SQLite write lock)."""

import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import QuotaUsage


async def _ensure_row(db: AsyncSession, user_id: uuid.UUID) -> None:
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(QuotaUsage).values(user_id=user_id, storage_bytes=0).on_conflict_do_nothing()
    )


async def reserve(db: AsyncSession, user_id: uuid.UUID, size: int, limit: int) -> None:
    await _ensure_row(db, user_id)
    result = await db.execute(
        update(QuotaUsage)
        .where(QuotaUsage.user_id == user_id, QuotaUsage.storage_bytes + size <= limit)
        .values(storage_bytes=QuotaUsage.storage_bytes + size)
        .returning(QuotaUsage.storage_bytes)
    )
    if result.first() is None:
        used = await usage(db, user_id)
        raise AppError(
            413,
            "quota_exceeded",
            "Not enough storage space left for this file",
            used_bytes=used,
            limit_bytes=limit,
        )


async def release(db: AsyncSession, user_id: uuid.UUID, size: int) -> None:
    await db.execute(
        update(QuotaUsage)
        .where(QuotaUsage.user_id == user_id)
        .values(storage_bytes=QuotaUsage.storage_bytes - size)
    )


async def usage(db: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await db.scalar(select(QuotaUsage.storage_bytes).where(QuotaUsage.user_id == user_id)) or 0
    )
