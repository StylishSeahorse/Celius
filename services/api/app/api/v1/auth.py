from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.sessions import SESSION_COOKIE, create_session, revoke_session
from app.config import Settings, get_settings
from app.db import get_db
from app.db.base import utcnow
from app.models import AuditEvent, User
from app.schemas.auth import LoginRequest, LoginResponse, MeResponse

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
    token, session = await create_session(
        db, user, settings, device_id=body.device_id, user_agent=request.headers.get("user-agent")
    )
    _audit(db, request, "auth.login", user, session_id=str(session.id))
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
