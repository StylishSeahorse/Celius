import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.models import Session, User

SESSION_COOKIE = "celius_session"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(
    db: AsyncSession,
    user: User,
    settings: Settings,
    *,
    device_id: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, Session]:
    token = secrets.token_urlsafe(32)
    session = Session(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=_hash_token(token),
        device_id=device_id,
        user_agent=(user_agent or "")[:512] or None,
        expires_at=utcnow() + timedelta(hours=settings.app_session_ttl_hours),
    )
    db.add(session)
    await db.flush()
    return token, session


async def resolve_session(db: AsyncSession, token: str) -> tuple[Session, User] | None:
    row = (
        await db.execute(
            select(Session, User)
            .join(User, User.id == Session.user_id)
            .where(Session.token_hash == _hash_token(token))
        )
    ).first()
    if row is None:
        return None
    session, user = row
    now = utcnow()
    expires_at = session.expires_at
    if expires_at.tzinfo is None:  # SQLite drops tz info
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    if session.revoked_at is not None or expires_at <= now or not user.is_active:
        return None
    return session, user


async def revoke_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(update(Session).where(Session.id == session_id).values(revoked_at=utcnow()))
