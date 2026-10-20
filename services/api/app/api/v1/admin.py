"""People and invitations, for administrators (A-01, A-02, A-03)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.db.base import utcnow
from app.errors import AppError
from app.models import Invitation, User
from app.services import users as svc
from app.services.audit import audit

router = APIRouter(prefix="/admin", tags=["admin"])
DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _admin(auth) -> User:
    if not auth.user.is_admin:
        raise AppError(403, "forbidden", "Only an administrator can manage people")
    return auth.user


class PersonOut(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str | None
    is_admin: bool
    is_active: bool
    created_at: datetime
    last_seen_at: datetime | None = None
    storage_used_bytes: int = 0
    storage_quota_bytes: int | None
    daily_message_limit: int | None
    allowed_models: list[str] | None
    allowed_tool_profiles: list[str] | None


class PersonUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    is_admin: bool | None = None
    is_active: bool | None = None
    storage_quota_bytes: int | None = Field(default=None, ge=0)
    daily_message_limit: int | None = Field(default=None, ge=0)
    allowed_models: list[str] | None = None
    allowed_tool_profiles: list[str] | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=12, max_length=1024)


class InvitationIn(BaseModel):
    note: str | None = Field(default=None, max_length=200)
    make_admin: bool = False
    expires_hours: int = Field(default=72, ge=1, le=24 * 30)


class InvitationOut(BaseModel):
    id: uuid.UUID
    note: str | None
    make_admin: bool
    state: str
    expires_at: datetime
    created_at: datetime
    used_by: str | None = None
    # Only when created: the link is shown once and never stored.
    url: str | None = None


def _person(user: User, seen: dict, used: dict) -> PersonOut:
    return PersonOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
        is_active=user.is_active,
        created_at=user.created_at,
        last_seen_at=seen.get(user.id),
        storage_used_bytes=used.get(user.id, 0),
        storage_quota_bytes=user.storage_quota_bytes,
        daily_message_limit=user.daily_message_limit,
        allowed_models=user.allowed_models,
        allowed_tool_profiles=user.allowed_tool_profiles,
    )


@router.get("/users", response_model=list[PersonOut])
async def list_people(auth: CurrentAuth, db: DB) -> list[PersonOut]:
    _admin(auth)
    people = (await db.scalars(select(User).order_by(User.username))).all()
    seen, used = await svc.last_seen(db), await svc.storage_used(db)
    return [_person(u, seen, used) for u in people]


@router.patch("/users/{user_id}", response_model=PersonOut)
async def update_person(
    user_id: uuid.UUID, body: PersonUpdate, auth: CurrentAuth, db: DB
) -> PersonOut:
    """Only the fields sent change; send `null` to clear a limit."""
    admin = _admin(auth)
    user = await svc.get_user(db, user_id)
    changes = body.model_dump(exclude_unset=True)
    for flag in ("is_admin", "is_active"):
        if flag in changes and changes[flag] is None:
            changes.pop(flag)
    await svc.update_user(db, admin, user, changes)
    audit(db, admin.id, "admin.user_updated", "user", user.id, fields=sorted(changes))
    await db.commit()
    return _person(user, await svc.last_seen(db), await svc.storage_used(db))


@router.post("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: uuid.UUID, body: PasswordReset, auth: CurrentAuth, db: DB
) -> None:
    """Set a new password for someone who forgot theirs; they are signed out everywhere."""
    admin = _admin(auth)
    user = await svc.get_user(db, user_id)
    revoked = await svc.reset_password(db, user, body.new_password)
    audit(db, admin.id, "admin.password_reset", "user", user.id, sessions_revoked=revoked)
    await db.commit()


@router.delete("/users/{user_id}")
async def delete_person(
    user_id: uuid.UUID, confirm: str, auth: CurrentAuth, db: DB, settings: AppSettings
) -> dict:
    """Delete an account and all its data. `confirm` must be the username."""
    admin = _admin(auth)
    user = await svc.get_user(db, user_id)
    if confirm != user.username:
        raise AppError(422, "confirm_username", "Type the username to confirm")
    audit(db, admin.id, "admin.user_deleted", username=user.username)
    return await svc.delete_user(db, admin, user, settings)


@router.get("/invitations", response_model=list[InvitationOut])
async def list_invitations(auth: CurrentAuth, db: DB) -> list[InvitationOut]:
    _admin(auth)
    rows = (
        await db.execute(
            select(Invitation, User.username)
            .outerjoin(User, User.id == Invitation.used_by)
            .order_by(Invitation.created_at.desc())
            .limit(100)
        )
    ).all()
    return [
        InvitationOut(
            id=i.id,
            note=i.note,
            make_admin=i.make_admin,
            state=svc.invitation_state(i),
            expires_at=i.expires_at,
            created_at=i.created_at,
            used_by=username,
        )
        for i, username in rows
    ]


@router.post("/invitations", response_model=InvitationOut, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: InvitationIn, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> InvitationOut:
    """A one-time link to create an account. Its URL is returned now and never again."""
    admin = _admin(auth)
    invitation, token = await svc.create_invitation(
        db, admin, note=body.note, make_admin=body.make_admin, hours=body.expires_hours
    )
    audit(db, admin.id, "admin.invitation_created", invitation_id=str(invitation.id))
    await db.commit()
    base = (settings.app_public_url or str(request.base_url)).rstrip("/")
    return InvitationOut(
        id=invitation.id,
        note=invitation.note,
        make_admin=invitation.make_admin,
        state="open",
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
        url=f"{base}/#/join/{token}",
    )


@router.delete("/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(invitation_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    admin = _admin(auth)
    invitation = await db.get(Invitation, invitation_id)
    if invitation is None:
        raise AppError(404, "not_found", "Invitation not found")
    if invitation.used_at is None and invitation.revoked_at is None:
        invitation.revoked_at = utcnow()
        audit(db, admin.id, "admin.invitation_revoked", invitation_id=str(invitation.id))
        await db.commit()
