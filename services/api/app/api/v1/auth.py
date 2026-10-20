from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.sessions import SESSION_COOKIE, create_session, revoke_all, revoke_session
from app.auth.throttle import login_blocked
from app.config import Settings, get_settings
from app.db import get_db
from app.db.base import utcnow
from app.models import AuditEvent, Session, User
from app.schemas.auth import (
    InvitationCheck,
    LoginRequest,
    LoginResponse,
    MeResponse,
    PasswordChange,
    RegisterRequest,
)
from app.services import users

router = APIRouter(prefix="/auth", tags=["auth"])


def _audit(db: AsyncSession, request: Request, action: str, user: User | None, **detail) -> None:
    db.add(
        AuditEvent(
            user_id=user.id if user else None,
            action=action,
            detail=detail,
            ip_address=request.client.host if request.client else None,
            created_at=utcnow(),
        )
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginResponse:
    ip = request.client.host if request.client else None
    if (wait := await login_blocked(db, settings, body.username, ip)) is not None:
        _audit(db, request, "auth.login_throttled", None, username=body.username[:64])
        await db.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed sign-in attempts; try again later",
            headers={"Retry-After": str(wait)},
        )
    user = await db.scalar(select(User).where(User.username == body.username))
    if not verify_password(user.password_hash if user else None, body.password) or (
        user is not None and not user.is_active
    ):
        _audit(db, request, "auth.login_failed", user, username=body.username[:64])
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")

    assert user is not None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    return await _sign_in(db, request, response, settings, user, body.device_id, "auth.login")


async def _sign_in(
    db: AsyncSession,
    request: Request,
    response: Response,
    settings: Settings,
    user: User,
    device_id: str | None,
    action: str,
) -> LoginResponse:
    token, session = await create_session(
        db, user, settings, device_id=device_id, user_agent=request.headers.get("user-agent")
    )
    _audit(db, request, action, user, session_id=str(session.id))
    await db.commit()

    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.app_session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return LoginResponse(
        user_id=user.id,
        username=user.username,
        access_token=token,
        expires_at=session.expires_at.isoformat(),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    auth: CurrentAuth,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await revoke_session(db, auth.session.id)
    _audit(db, request, "auth.logout", auth.user, session_id=str(auth.session.id))
    await db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange,
    auth: CurrentAuth,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Change the password; every other session (other devices) is signed out."""
    if not verify_password(auth.user.password_hash, body.current_password):
        _audit(db, request, "auth.password_change_failed", auth.user)
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The current password is wrong")
    user = await db.get(User, auth.user.id)
    user.password_hash = hash_password(body.new_password)
    revoked = await revoke_all(db, user.id, except_session=auth.session.id)
    _audit(db, request, "auth.password_changed", user, other_sessions_revoked=revoked)
    await db.commit()


@router.get("/sessions")
async def list_sessions(
    auth: CurrentAuth, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[dict]:
    """Signed-in devices."""
    rows = (
        await db.scalars(
            select(Session)
            .where(
                Session.user_id == auth.user.id,
                Session.revoked_at.is_(None),
                Session.expires_at > utcnow(),
            )
            .order_by(Session.created_at.desc())
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "device_id": row.device_id,
            "user_agent": row.user_agent,
            "created_at": row.created_at.isoformat(),
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
            "current": row.id == auth.session.id,
        }
        for row in rows
    ]


@router.post("/sessions/revoke-others", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_other_sessions(
    auth: CurrentAuth, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    revoked = await revoke_all(db, auth.user.id, except_session=auth.session.id)
    _audit(db, request, "auth.sessions_revoked", auth.user, count=revoked)
    await db.commit()


@router.get("/me", response_model=MeResponse)
async def me(auth: CurrentAuth, settings: Annotated[Settings, Depends(get_settings)]) -> MeResponse:
    return MeResponse(
        user_id=auth.user.id,
        username=auth.user.username,
        display_name=auth.user.display_name,
        is_admin=auth.user.is_admin,
        single_user_mode=settings.app_single_user_mode,
        capabilities={
            "memory": settings.memory_enabled,
            "tools": True,
            "sandbox": bool(settings.sandbox_url and settings.sandbox_token),
            "files": True,
        },
    )


@router.get("/invitations/{token}", response_model=InvitationCheck)
async def check_invitation(
    token: str, db: Annotated[AsyncSession, Depends(get_db)]
) -> InvitationCheck:
    """Whether an invitation link can still be used (for the sign-up page)."""
    invitation = await users.open_invitation(db, token)
    return InvitationCheck(expires_at=invitation.expires_at, note=invitation.note)


@router.post("/register", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginResponse:
    """Create an account with an invitation, and sign in."""
    user = await users.register(db, body.token, body.username, body.password, body.display_name)
    return await _sign_in(db, request, response, settings, user, body.device_id, "auth.registered")
