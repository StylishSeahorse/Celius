"""Web Push subscriptions for the signed-in user's devices."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError
from app.models import PushSubscription
from app.services import push

router = APIRouter(prefix="/push", tags=["push"])
DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=16, max_length=100)


class SubscriptionIn(BaseModel):
    """`PushSubscription.toJSON()` from the browser."""

    endpoint: str = Field(min_length=10, max_length=2000)
    keys: PushKeys


class Unsubscribe(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000)


class PushInfo(BaseModel):
    enabled: bool
    public_key: str
    devices: int
    subscribed: bool = False


@router.get("", response_model=PushInfo)
async def push_info(
    auth: CurrentAuth, db: DB, settings: AppSettings, endpoint: str | None = None
) -> PushInfo:
    """The key to subscribe with, and how many of your devices get notifications."""
    endpoints = (
        await db.scalars(
            select(PushSubscription.endpoint).where(PushSubscription.user_id == auth.user.id)
        )
    ).all()
    return PushInfo(
        enabled=settings.push_enabled,
        public_key=push.public_key(settings),
        devices=len(endpoints),
        subscribed=endpoint in endpoints if endpoint else False,
    )


@router.post("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def subscribe(
    body: SubscriptionIn, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> None:
    if not settings.push_enabled:
        raise AppError(409, "push_disabled", "Notifications are switched off on this server")
    try:
        await push.subscribe(
            db,
            auth.user.id,
            settings,
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
            user_agent=request.headers.get("user-agent"),
        )
    except push.InvalidSubscription as exc:
        raise AppError(422, "invalid_subscription", str(exc)) from exc


@router.post("/subscriptions/remove", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(body: Unsubscribe, auth: CurrentAuth, db: DB) -> None:
    await push.unsubscribe(db, auth.user.id, body.endpoint)


@router.post("/test", status_code=status.HTTP_202_ACCEPTED)
async def send_test(auth: CurrentAuth, db: DB) -> dict:
    """Send "Notifications work" to every device of yours."""
    push.queue(db, auth.user.id, title="Celius", body="Notifications work.", tag="test")
    await db.commit()
    return {"queued": True}
