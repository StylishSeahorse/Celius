"""Projection of an external agent's events into the conversation (filled in Phase 4)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RemoteSession, RemoteSessionEvent


async def project(db: AsyncSession, session: RemoteSession, event: RemoteSessionEvent) -> None:
    return None
