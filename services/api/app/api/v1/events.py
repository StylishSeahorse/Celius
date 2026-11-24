"""The event finder: sources, scans, suggestions, and Sign in with Meta (ADR C-17)."""

import uuid
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError
from app.models import EventSource, EventSuggestion, User
from app.services.audit import audit
from app.services.events import facebook
from app.services.events import service as events
from app.services.integrations import accounts, meta

router = APIRouter(tags=["events"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


class SourceIn(BaseModel):
    platform: Literal["instagram", "facebook"]
    handle: str = Field(min_length=1, max_length=300)
    label: str | None = Field(default=None, max_length=200)


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    label: str | None = Field(default=None, max_length=200)


class SourceOut(BaseModel):
    id: uuid.UUID
    platform: str
    handle: str
    label: str | None
    enabled: bool
    last_checked_at: datetime | None
    last_error: str | None


class SuggestionOut(BaseModel):
    id: uuid.UUID
    status: str
    title: str
    starts_at: datetime
    ends_at: datetime | None
    all_day: bool
    venue: str | None
    address: str | None
    price: str | None
    summary: str | None
    ticket_url: str | None
    post_url: str | None
    image_url: str | None
    also_posted: list[str]
    source: str | None


class EventSettings(BaseModel):
    every_days: int
    calendar: str | None
    scanned_at: datetime | None
    scanning: bool
    meta_configured: bool
    meta_connected: bool
    meta_account: str | None
    meta_error: str | None
    facebook_configured: bool


class EventSettingsUpdate(BaseModel):
    every_days: int | None = Field(default=None, ge=1, le=60)
    calendar: str | None = Field(default=None, max_length=64)


class AddToCalendar(BaseModel):
    calendar: str | None = Field(default=None, max_length=64)


class MetaStartOut(BaseModel):
    url: str


def _source_out(row: EventSource) -> SourceOut:
    return SourceOut(
        id=row.id,
        platform=row.platform,
        handle=row.handle,
        label=row.label,
        enabled=row.enabled,
        last_checked_at=row.last_checked_at,
        last_error=row.last_error,
    )


def _suggestion_out(row: EventSuggestion, sources: dict) -> SuggestionOut:
    source = sources.get(row.source_id)
    return SuggestionOut(
        id=row.id,
        status=row.status,
        title=row.title,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        all_day=row.all_day,
        venue=row.venue,
        address=row.address,
        price=row.price,
        summary=row.summary,
        ticket_url=row.ticket_url,
        post_url=row.post_url,
        image_url=row.image_url,
        also_posted=list(row.also_posted or []),
        source=(source.label or source.handle) if source else None,
    )


# ---------------------------------------------------------------------- settings


@router.get("/events/settings", response_model=EventSettings)
async def get_event_settings(auth: CurrentAuth, db: DB, settings: AppSettings) -> EventSettings:
    saved = await events.prefs(db, auth.user.id, settings)
    account = await events.meta_account(db, auth.user.id)
    return EventSettings(
        every_days=saved.every_days,
        calendar=saved.calendar,
        scanned_at=saved.scanned_at,
        scanning=await events.scanning(db, auth.user.id),
        meta_configured=meta.configured(settings),
        meta_connected=account is not None,
        meta_account=(f"@{account.config.get('ig_username')}" if account else None),
        meta_error=account.last_error if account else None,
        facebook_configured=facebook.configured(settings),
    )


@router.put("/events/settings", response_model=EventSettings)
async def update_event_settings(
    body: EventSettingsUpdate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> EventSettings:
    changes: dict = {}
    if body.every_days is not None:
        changes["every_days"] = body.every_days
    if body.calendar is not None:
        if body.calendar != "local":
            await accounts.owned(db, auth.user.id, body.calendar, kind="calendar")
        changes["calendar"] = body.calendar
    if changes:
        await events.save_prefs(db, auth.user.id, **changes)
        await db.commit()
    return await get_event_settings(auth, db, settings)


@router.post("/events/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_now(auth: CurrentAuth, db: DB) -> dict:
    queued = await events.queue_scan(db, auth.user.id)
    await db.commit()
    return {"queued": queued}


# ---------------------------------------------------------------------- sources


@router.get("/events/sources", response_model=list[SourceOut])
async def list_sources(auth: CurrentAuth, db: DB) -> list[SourceOut]:
    rows = (
        await db.scalars(
            select(EventSource)
            .where(EventSource.owner_id == auth.user.id)
            .order_by(EventSource.created_at)
        )
    ).all()
    return [_source_out(r) for r in rows]


@router.post("/events/sources", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(body: SourceIn, auth: CurrentAuth, db: DB) -> SourceOut:
    source = await events.add_source(db, auth.user.id, body.platform, body.handle, body.label)
    await db.commit()
    return _source_out(source)


@router.patch("/events/sources/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: uuid.UUID, body: SourceUpdate, auth: CurrentAuth, db: DB
) -> SourceOut:
    source = await events.owned_source(db, auth.user.id, source_id)
    if body.enabled is not None:
        source.enabled = body.enabled
    if body.label is not None:
        source.label = body.label.strip() or None
    await db.commit()
    return _source_out(source)


@router.delete("/events/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    source = await events.owned_source(db, auth.user.id, source_id)
    await db.delete(source)
    await db.commit()


# ---------------------------------------------------------------------- suggestions


@router.get("/events/suggestions", response_model=list[SuggestionOut])
async def list_suggestions(
    auth: CurrentAuth,
    db: DB,
    status_filter: Annotated[
        Literal["pending", "added", "dismissed"], Query(alias="status")
    ] = "pending",
) -> list[SuggestionOut]:
    rows = (
        await db.scalars(
            select(EventSuggestion)
            .where(
                EventSuggestion.owner_id == auth.user.id,
                EventSuggestion.status == status_filter,
            )
            .order_by(EventSuggestion.starts_at)
            .limit(200)
        )
    ).all()
    sources = {
        s.id: s
        for s in (
            await db.scalars(select(EventSource).where(EventSource.owner_id == auth.user.id))
        ).all()
    }
    return [_suggestion_out(r, sources) for r in rows]


@router.post("/events/suggestions/{suggestion_id}/add", response_model=SuggestionOut)
async def add_suggestion(
    suggestion_id: uuid.UUID,
    body: AddToCalendar,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
) -> SuggestionOut:
    row = await events.owned_suggestion(db, auth.user.id, suggestion_id)
    await events.add_to_calendar(db, auth.user.id, row, settings, body.calendar)
    audit(db, auth.user.id, "event.added", "event_suggestion", row.id, title=row.title)
    await db.commit()
    return _suggestion_out(row, {})


@router.post("/events/suggestions/{suggestion_id}/dismiss", response_model=SuggestionOut)
async def dismiss_suggestion(suggestion_id: uuid.UUID, auth: CurrentAuth, db: DB) -> SuggestionOut:
    row = await events.owned_suggestion(db, auth.user.id, suggestion_id)
    if row.status == "added":
        raise AppError(409, "already_added", "Remove it from the calendar instead")
    row.status = "dismissed"
    await db.commit()
    return _suggestion_out(row, {})


# ---------------------------------------------------------------------- Sign in with Meta


@router.post("/integrations/meta/start", response_model=MetaStartOut)
async def meta_start(auth: CurrentAuth, settings: AppSettings, response: Response) -> MetaStartOut:
    try:
        url, nonce = meta.start(settings, auth.user.id)
    except meta.MetaError as exc:
        raise AppError(409, "meta_not_configured", str(exc)) from exc
    response.set_cookie(
        meta.NONCE_COOKIE,
        nonce,
        max_age=meta.STATE_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",  # sent on Meta's redirect back (a top-level GET)
        path=meta.CALLBACK_PATH,
    )
    return MetaStartOut(url=url)


def _back(outcome: str, text: str = "") -> RedirectResponse:
    query = urlencode({"tab": "events", "meta": outcome, **({"message": text} if text else {})})
    response = RedirectResponse(f"/#/today?{query}", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(meta.NONCE_COOKIE, path=meta.CALLBACK_PATH)
    return response


@router.get("/integrations/meta/callback", include_in_schema=False)
async def meta_callback(
    request: Request,
    db: DB,
    settings: AppSettings,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Where Meta sends the browser back. Like Google's: the session cookie is Strict, so
    the user comes from the sealed state, bound to this browser's nonce."""
    if error:
        return _back("cancelled" if error == "access_denied" else "error", error[:100])
    try:
        user_id = meta.check_state(settings, state or "", request.cookies.get(meta.NONCE_COOKIE))
        user = await db.get(User, user_id)
        if user is None or not user.is_active:
            raise meta.MetaError("This Celius account can't connect")
        if not code:
            raise meta.MetaError("Meta didn't send a sign-in code")
        config, credentials = await meta.connect(settings, code)
    except meta.MetaError as exc:
        return _back("error", str(exc))
    account = await events.meta_account(db, user.id)
    if account is None:
        account = await accounts.create(
            db,
            user.id,
            kind="social",
            provider="meta",
            name=f"Instagram (@{config['ig_username']})",
            config=config,
            password=None,
            allow_private_network=False,
            settings=settings,
        )
    else:
        account.config = config
        account.enabled = True
        account.last_error = None
    accounts.seal_credentials(settings, account, credentials)
    audit(db, user.id, "integration.meta_connected", "integration", account.id)
    await db.commit()
    return _back("connected", f"@{config['ig_username']}")
