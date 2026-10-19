from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

SessionFactory = async_sessionmaker[AsyncSession]


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> SessionFactory:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


def get_session_factory() -> SessionFactory:
    """Dependency for code that opens its own short-lived sessions (e.g. SSE streams)."""
    return get_sessionmaker()


async def get_db(
    factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
