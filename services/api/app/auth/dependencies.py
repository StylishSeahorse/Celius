from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import SESSION_COOKIE, resolve_session
from app.db import get_db
from app.models import Session, User


@dataclass
class AuthContext:
    user: User
    session: Session


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return request.cookies.get(SESSION_COOKIE)


async def require_auth(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> AuthContext:
    token = _extract_token(request)
    resolved = await resolve_session(db, token) if token else None
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    session, user = resolved
    return AuthContext(user=user, session=session)


CurrentAuth = Annotated[AuthContext, Depends(require_auth)]
