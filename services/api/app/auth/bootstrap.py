import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password
from app.config import Settings
from app.models import User, UserSettings

log = logging.getLogger(__name__)


async def ensure_bootstrap_user(db: AsyncSession, settings: Settings) -> User | None:
    """Create the initial account if the database has no users and credentials are configured.

    Never overwrites an existing account; changing the password later is an explicit action.
    """
    if not settings.bootstrap_username or settings.bootstrap_password is None:
        return None
    existing = await db.scalar(select(func.count()).select_from(User))
    if existing:
        return None
    password = settings.bootstrap_password.get_secret_value()
    if len(password) < 12:
        raise ValueError("BOOTSTRAP_PASSWORD must be at least 12 characters")
    user = User(
        username=settings.bootstrap_username, password_hash=hash_password(password), is_admin=True
    )
    db.add(user)
    await db.flush()
    db.add(
        UserSettings(user_id=user.id, default_model_id=settings.model_default_id, preferences={})
    )
    await db.commit()
    log.info("Created bootstrap user %r", user.username)
    return user
