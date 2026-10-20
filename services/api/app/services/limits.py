"""Per-user limits set by an administrator (A-03). None means the server default (storage)
or no limit (the rest). Administrators can set limits on themselves too."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.errors import AppError
from app.models import Run, User

# Narrowest first. read_only is always allowed: it can't change anything.
PROFILE_ORDER = ("read_only", "standard", "developer", "administrator")


def storage_limit(user: User, settings: Settings) -> int:
    return (
        user.storage_quota_bytes
        if user.storage_quota_bytes is not None
        else settings.max_user_storage_bytes
    )


def model_allowed(user: User, model_id: str) -> bool:
    return user.allowed_models is None or model_id in user.allowed_models


def profile_allowed(user: User | None, base: str) -> bool:
    if user is None or user.allowed_tool_profiles is None or base == "read_only":
        return True
    return base in user.allowed_tool_profiles


def widest_allowed_profile(user: User, wanted: str) -> str:
    """The widest allowed profile that isn't wider than the one wanted."""
    allowed = [p for p in PROFILE_ORDER if profile_allowed(user, p)]
    narrower = PROFILE_ORDER[: PROFILE_ORDER.index(wanted) + 1] if wanted in PROFILE_ORDER else ()
    candidates = [p for p in allowed if p in narrower]
    return candidates[-1] if candidates else "read_only"


async def check_daily_messages(db: AsyncSession, user: User) -> None:
    """A reply counts against the limit (sends, edits and regenerations alike)."""
    if user.daily_message_limit is None:
        return
    since = utcnow() - timedelta(hours=24)
    used = await db.scalar(
        select(func.count()).select_from(Run).where(Run.owner_id == user.id, Run.created_at > since)
    )
    if used >= user.daily_message_limit:
        raise AppError(
            429,
            "daily_limit",
            f"You've reached your limit of {user.daily_message_limit} replies in 24 hours",
            limit=user.daily_message_limit,
        )


async def load(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)
