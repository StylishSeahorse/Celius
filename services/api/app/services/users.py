"""Accounts for a household or small team (A-01, A-02): invitations, and what an
administrator can do to other accounts."""

import hashlib
import secrets
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

import anyio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password
from app.auth.sessions import revoke_all
from app.config import Settings
from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import (
    Conversation,
    Invitation,
    Project,
    QuotaUsage,
    Session,
    StoredFile,
    User,
)
from app.services.files.service import storage_for
from app.services.limits import PROFILE_ORDER



def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------- invitations


async def create_invitation(
    db: AsyncSession, admin: User, *, note: str | None, make_admin: bool, hours: int
) -> tuple[Invitation, str]:
    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        id=uuid.uuid4(),
        token_hash=_hash(token),
        created_by=admin.id,
        note=note,
        make_admin=make_admin,
        expires_at=utcnow() + timedelta(hours=hours),
    )
    db.add(invitation)
    await db.flush()
    return invitation, token


def invitation_state(invitation: Invitation) -> str:
    if invitation.revoked_at is not None:
        return "revoked"
    if invitation.used_at is not None:
        return "used"
    expires = invitation.expires_at
    if expires.tzinfo is None:  # SQLite
        expires = expires.replace(tzinfo=utcnow().tzinfo)
    return "expired" if expires <= utcnow() else "open"


async def open_invitation(db: AsyncSession, token: str) -> Invitation:
    invitation = await db.scalar(
        select(Invitation).where(Invitation.token_hash == _hash(token)).with_for_update()
    )
    if invitation is None or invitation_state(invitation) != "open":
        raise AppError(404, "invitation_invalid", "This invitation link is not valid any more")
    return invitation


async def register(
    db: AsyncSession, token: str, username: str, password: str, display_name: str | None
) -> User:
    """Create an account from an invitation; the invitation can't be used again."""
    invitation = await open_invitation(db, token)
    username = username.strip().lower()
    taken = await db.scalar(select(User.id).where(func.lower(User.username) == username))
    if taken is not None:
        raise AppError(409, "username_taken", "That username is taken")
    user = User(
        id=uuid.uuid4(),
        username=username,
        display_name=(display_name or "").strip() or None,
        password_hash=hash_password(password),
        is_admin=invitation.make_admin,
    )
    db.add(user)
    await db.flush()
    invitation.used_at = utcnow()
    invitation.used_by = user.id
    return user


# ---------------------------------------------------------------------- administration


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise not_found("User")
    return user


async def active_admins(db: AsyncSession) -> int:
    return await db.scalar(
        select(func.count()).select_from(User).where(User.is_admin, User.is_active)
    )


async def update_user(db: AsyncSession, admin: User, user: User, changes: dict) -> User:
    if user.id == admin.id and (
        changes.get("is_admin") is False or changes.get("is_active") is False
    ):
        raise AppError(409, "own_account", "You can't remove your own access")
    for profile in changes.get("allowed_tool_profiles") or []:
        if profile not in PROFILE_ORDER:
            raise AppError(422, "unknown_profile", f"Unknown tool profile {profile!r}")
    for key, value in changes.items():
        setattr(user, key, value)
    if changes.get("is_active") is False:
        await revoke_all(db, user.id)  # signed out everywhere, at once
    if user.is_admin is False and await active_admins(db) == 0:
        raise AppError(409, "last_admin", "There must be at least one administrator")
    return user


async def reset_password(db: AsyncSession, user: User, password: str) -> int:
    user.password_hash = hash_password(password)
    return await revoke_all(db, user.id)


async def last_seen(db: AsyncSession) -> dict[uuid.UUID, object]:
    rows = await db.execute(
        select(Session.user_id, func.max(Session.last_seen_at)).group_by(Session.user_id)
    )
    return dict(rows.all())


async def storage_used(db: AsyncSession) -> dict[uuid.UUID, int]:
    rows = await db.execute(select(QuotaUsage.user_id, QuotaUsage.storage_bytes))
    return dict(rows.all())


async def delete_user(db: AsyncSession, admin: User, user: User, settings: Settings) -> dict:
    """Delete an account and everything in it: chats, files, memories, projects (their
    shares too), schedules, sessions. Shared file content stays while others use it."""
    if user.id == admin.id:
        raise AppError(409, "own_account", "You can't delete your own account")
    if user.is_admin and user.is_active and await active_admins(db) <= 1:
        raise AppError(409, "last_admin", "There must be at least one administrator")
    files = (
        await db.execute(
            select(StoredFile.id, StoredFile.sha256).where(StoredFile.owner_id == user.id)
        )
    ).all()
    conversations = (
        await db.scalars(select(Conversation.id).where(Conversation.owner_id == user.id))
    ).all()
    projects = (await db.scalars(select(Project.id).where(Project.owner_id == user.id))).all()
    # Others' chats in this user's projects stay, unfiled (the FK sets them to NULL).
    await db.execute(delete(User).where(User.id == user.id))  # everything else cascades
    await db.flush()
    still_used = set(
        (
            await db.scalars(
                select(StoredFile.sha256).where(
                    StoredFile.sha256.in_([sha for _, sha in files]),
                    StoredFile.deleted_at.is_(None),
                )
            )
        ).all()
    )
    await db.commit()

    store = storage_for(settings)
    workspaces = Path(settings.workspace_root)

    def remove_content() -> None:
        for file_id, sha in files:
            store.thumbnail(file_id).unlink(missing_ok=True)
            if sha not in still_used:
                store.remove_blob(sha)
        for conversation_id in conversations:
            shutil.rmtree(workspaces / str(conversation_id), ignore_errors=True)

    await anyio.to_thread.run_sync(remove_content)
    return {"files": len(files), "conversations": len(conversations), "projects": len(projects)}
