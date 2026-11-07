"""Calendar and email accounts, calendar events, email, automations (ADR C-11)."""

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError, not_found
from app.models import Automation, AutomationRun, IntegrationAccount, User
from app.schemas.integrations import (
    AccountCheck,
    AccountCreate,
    AccountOut,
    AccountUpdate,
    AutomationCreate,
    AutomationOut,
    AutomationRunOut,
    AutomationUpdate,
    EmailCompose,
    EmailOut,
    EmailSummaryOut,
    EventChange,
    EventCreate,
    EventList,
    EventOut,
    EventRef,
)
from app.services.assistant import tasks as user_tasks
from app.services.audit import audit
from app.services.automation import service as automation_svc
from app.services.integrations import accounts, google
from app.services.integrations import calendar as cal
from app.services.integrations.email import EmailError, Mailbox
from app.services.sync import record_sync

router = APIRouter(tags=["integrations"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


async def _tz(db, owner_id, settings):
    return user_tasks.zone((await user_tasks.user_prefs(db, owner_id, settings)).timezone)


# ---------------------------------------------------------------------- accounts


def _account_out(account: IntegrationAccount) -> AccountOut:
    return AccountOut(
        id=account.id,
        kind=account.kind,
        provider=account.provider,
        name=account.name,
        config=account.config,
        has_password=bool(account.credentials),
        allow_private_network=account.allow_private_network,
        enabled=account.enabled,
        last_error=account.last_error,
        last_checked_at=account.last_checked_at,
    )


@router.get("/integrations/accounts", response_model=list[AccountOut])
async def list_accounts(auth: CurrentAuth, db: DB) -> list[AccountOut]:
    rows = (
        await db.scalars(
            select(IntegrationAccount)
            .where(IntegrationAccount.owner_id == auth.user.id)
            .order_by(IntegrationAccount.created_at)
        )
    ).all()
    return [_account_out(a) for a in rows]


@router.post(
    "/integrations/accounts", response_model=AccountOut, status_code=status.HTTP_201_CREATED
)
async def create_account(
    body: AccountCreate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> AccountOut:
    if body.allow_private_network and not auth.user.is_admin:
        # Like MCP servers: reaching the server's own network is an administrator's call.
        raise AppError(403, "admin_only", "Only an administrator can allow the local network")
    account = await accounts.create(
        db,
        auth.user.id,
        kind=body.kind,
        provider=body.provider,
        name=body.name,
        config=body.config,
        password=body.password,
        allow_private_network=body.allow_private_network,
        settings=settings,
    )
    await db.commit()
    return _account_out(account)


@router.patch("/integrations/accounts/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: uuid.UUID, body: AccountUpdate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> AccountOut:
    account = await accounts.owned(db, auth.user.id, account_id)
    if body.allow_private_network and not auth.user.is_admin:
        raise AppError(403, "admin_only", "Only an administrator can allow the local network")
    if account.provider == "gmail" and (body.config is not None or body.password is not None):
        raise AppError(
            422, "oauth_account", "A Gmail account's servers are fixed; it signs in with Google"
        )
    if body.name is not None:
        account.name = body.name.strip()
    if body.config is not None:
        account.config = accounts.clean_config(account.provider, {**account.config, **body.config})
    if body.password is not None:
        accounts.set_password(settings, account, body.password or None)
    if body.allow_private_network is not None:
        account.allow_private_network = body.allow_private_network
    if body.enabled is not None:
        account.enabled = body.enabled
    audit(db, auth.user.id, "integration.updated", "integration", account.id)
    await db.commit()
    return _account_out(account)


@router.post("/integrations/accounts/{account_id}/check", response_model=AccountCheck)
async def check_account(
    account_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> AccountCheck:
    """Connect and log in, without changing anything."""
    account = await accounts.owned(db, auth.user.id, account_id)
    error = None
    try:
        if account.kind == "calendar":
            async with cal.CalDAV(account, settings) as client:
                await client.check()
        else:
            await Mailbox(account, settings).check()
    except (cal.CalendarError, EmailError, AppError) as exc:
        error = getattr(exc, "message", None) or str(exc)
    account.last_error = error[:500] if error else None
    account.last_checked_at = datetime.now(UTC)
    await db.commit()
    return AccountCheck(ok=error is None, error=error)


@router.delete("/integrations/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> None:
    account = await accounts.owned(db, auth.user.id, account_id)
    if account.provider == "gmail":
        try:
            await google.revoke(accounts.credentials(settings, account))
        except AppError:
            pass  # unreadable credentials: nothing to revoke
        google.forget(account.id)
    await db.delete(account)
    audit(db, auth.user.id, "integration.deleted", "integration", account.id)
    await db.commit()


# ---------------------------------------------------------------------- Sign in with Google


class GoogleStatus(BaseModel):
    configured: bool
    redirect_uri: str


class GoogleStart(BaseModel):
    login_hint: str | None = Field(default=None, max_length=320)


class GoogleStartOut(BaseModel):
    url: str


@router.get("/integrations/google", response_model=GoogleStatus)
async def google_status(auth: CurrentAuth, settings: AppSettings) -> GoogleStatus:
    return GoogleStatus(
        configured=google.configured(settings), redirect_uri=google.redirect_uri(settings)
    )


@router.post("/integrations/google/start", response_model=GoogleStartOut)
async def google_start(
    body: GoogleStart, auth: CurrentAuth, settings: AppSettings, response: Response
) -> GoogleStartOut:
    """The Google consent URL. The browser goes there; Google sends it back to the callback."""
    try:
        url, nonce = google.start(settings, auth.user.id, body.login_hint)
    except google.GoogleError as exc:
        raise AppError(409, "google_not_configured", str(exc)) from exc
    response.set_cookie(
        google.NONCE_COOKIE,
        nonce,
        max_age=google.STATE_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",  # sent on Google's redirect back (a top-level GET)
        path=google.CALLBACK_PATH,
    )
    return GoogleStartOut(url=url)


def _back(outcome: str, text: str = "") -> RedirectResponse:
    query = urlencode({"google": outcome, **({"message": text} if text else {})})
    response = RedirectResponse(f"/#/today?{query}", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(google.NONCE_COOKIE, path=google.CALLBACK_PATH)
    return response


@router.get("/integrations/google/callback", include_in_schema=False)
async def google_callback(
    request: Request,
    db: DB,
    settings: AppSettings,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Where Google sends the browser back. The session cookie (SameSite=Strict) isn't
    sent here, so the user comes from the sealed state, bound to this browser's nonce."""
    if error:
        return _back("cancelled" if error == "access_denied" else "error", error[:100])
    try:
        user_id, verifier = google.check_state(
            settings, state or "", request.cookies.get(google.NONCE_COOKIE)
        )
        user = await db.get(User, user_id)
        if user is None or not user.is_active:
            raise google.GoogleError("This Celius account can't connect mail")
        if not code:
            raise google.GoogleError("Google didn't send a sign-in code")
        grant = await google.exchange(settings, code, verifier)
        existing = next(
            (
                a
                for a in await accounts.accounts_of(db, user.id, "email", enabled_only=False)
                if a.provider == "gmail" and a.config.get("username") == grant.email
            ),
            None,
        )
        previous = None
        if existing is not None:
            try:
                previous = accounts.credentials(settings, existing)
            except AppError:
                previous = None
        stored = google.sealed_credentials(grant, previous)
    except google.GoogleError as exc:
        return _back("error", str(exc))
    if existing is None:
        account = await accounts.create(
            db,
            user.id,
            kind="email",
            provider="gmail",
            name=f"Gmail ({grant.email})",
            config={"username": grant.email},
            password=None,
            allow_private_network=False,
            settings=settings,
        )
    else:
        account = existing
        account.enabled = True
        account.last_error = None
    accounts.seal_credentials(settings, account, stored)
    google.forget(account.id)
    audit(db, user.id, "integration.google_connected", "integration", account.id)
    await db.commit()
    return _back("connected", grant.email)


# ---------------------------------------------------------------------- calendar


def _event_out(event: cal.Event) -> EventOut:
    return EventOut(
        id=event.id,
        calendar=event.calendar,
        title=event.title,
        start=event.start,
        end=event.end,
        all_day=event.all_day,
        location=event.location,
        description=event.description,
        editable=event.editable,
        task_id=event.task_id,
    )


@router.get("/calendar/events", response_model=EventList)
async def calendar_events(
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    start: Annotated[str | None, Query(max_length=10)] = None,
    days: Annotated[int, Query(ge=1, le=62)] = 7,
) -> EventList:
    tz = await _tz(db, auth.user.id, settings)
    try:
        first = (
            datetime.fromisoformat(start).date()
            if start
            else datetime.now(UTC).astimezone(tz).date()
        )
    except ValueError as exc:
        raise AppError(422, "invalid_date", "start must be YYYY-MM-DD") from exc
    begin = datetime.combine(first, time(0, 0), tz).astimezone(UTC)
    events, problems = await cal.list_events(
        db, auth.user.id, begin, begin + timedelta(days=days), settings, tz
    )
    await db.commit()
    return EventList(
        events=[_event_out(e) for e in events],
        conflicts=[[a.id, b.id] for a, b in cal.conflicts(events)],
        problems=problems,
    )


@router.post("/calendar/events", response_model=EventOut, status_code=status.HTTP_201_CREATED)
async def create_event(
    body: EventCreate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> EventOut:
    tz = await _tz(db, auth.user.id, settings)
    start, all_day = user_tasks.parse_due(body.start, tz)
    end = (
        user_tasks.parse_due(body.end, tz)[0]
        if body.end
        else start + (timedelta(days=1) if all_day else timedelta(hours=1))
    )
    if body.task_id and await user_tasks._owned_task(db, auth.user.id, body.task_id) is None:
        raise not_found("Task")
    event = await cal.create_event(
        db,
        auth.user.id,
        title=body.title,
        start=start,
        end=end,
        all_day=all_day,
        settings=settings,
        tz=tz,
        calendar=body.calendar,
        location=body.location,
        description=body.description,
        task_id=body.task_id,
    )
    record_sync(db, auth.user.id, "calendar_event", uuid.uuid4())
    await db.commit()
    return _event_out(event)


@router.post("/calendar/events/update", status_code=status.HTTP_204_NO_CONTENT)
async def update_event(body: EventChange, auth: CurrentAuth, db: DB, settings: AppSettings) -> None:
    tz = await _tz(db, auth.user.id, settings)
    changes: dict = {}
    for key in ("title", "location", "description"):
        if getattr(body, key) is not None:
            changes[key] = getattr(body, key)
    if body.start:
        changes["start"], changes["all_day"] = user_tasks.parse_due(body.start, tz)
    if body.end:
        changes["end"] = user_tasks.parse_due(body.end, tz)[0]
    await cal.update_event(db, auth.user.id, body.id, changes, settings, tz)
    record_sync(db, auth.user.id, "calendar_event", uuid.uuid4())
    await db.commit()


@router.post("/calendar/events/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(body: EventRef, auth: CurrentAuth, db: DB, settings: AppSettings) -> None:
    await cal.delete_event(db, auth.user.id, body.id, settings)
    record_sync(db, auth.user.id, "calendar_event", uuid.uuid4())
    await db.commit()


# ---------------------------------------------------------------------- email


async def _mailbox(db, owner_id, account_id, settings) -> Mailbox:
    return Mailbox(await accounts.owned(db, owner_id, account_id, kind="email"), settings)


def _email_error(exc: EmailError) -> AppError:
    return AppError(502, "email_error", str(exc))


@router.get("/email/messages", response_model=list[EmailSummaryOut])
async def search_email(
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    account_id: uuid.UUID,
    q: Annotated[str | None, Query(max_length=200)] = None,
    unread: bool = False,
    since_days: Annotated[int | None, Query(ge=1, le=365)] = None,
) -> list[EmailSummaryOut]:
    mailbox = await _mailbox(db, auth.user.id, account_id, settings)
    try:
        found = await mailbox.search(text=q, unread_only=unread, since_days=since_days)
    except EmailError as exc:
        raise _email_error(exc) from exc
    return [
        EmailSummaryOut(
            id=m.uid,
            sender=m.sender,
            to=m.to,
            subject=m.subject,
            date=m.date,
            snippet=m.snippet,
            unread=m.unread,
        )
        for m in found
    ]


@router.get("/email/messages/{email_id}", response_model=EmailOut)
async def read_email(
    email_id: str, auth: CurrentAuth, db: DB, settings: AppSettings, account_id: uuid.UUID
) -> EmailOut:
    mailbox = await _mailbox(db, auth.user.id, account_id, settings)
    try:
        m = await mailbox.read(email_id)
    except EmailError as exc:
        raise _email_error(exc) from exc
    return EmailOut(
        id=m.uid,
        sender=m.sender,
        to=m.to,
        cc=m.cc,
        subject=m.subject,
        date=m.date,
        snippet=m.snippet,
        unread=m.unread,
        body=m.body,
        attachments=m.attachments,
    )


async def _compose(body: EmailCompose, owner_id, db, settings):
    mailbox = await _mailbox(db, owner_id, body.account_id, settings)
    try:
        original = await mailbox.read(body.reply_to) if body.reply_to else None
        to = body.to or ([original.sender] if original else [])
        return mailbox, mailbox.compose(to, body.subject, body.body, cc=body.cc, reply_to=original)
    except EmailError as exc:
        raise _email_error(exc) from exc


@router.post("/email/drafts", status_code=status.HTTP_201_CREATED)
async def save_draft(body: EmailCompose, auth: CurrentAuth, db: DB, settings: AppSettings) -> dict:
    mailbox, message = await _compose(body, auth.user.id, db, settings)
    try:
        await mailbox.save_draft(message)
    except EmailError as exc:
        raise _email_error(exc) from exc
    return {"saved": True, "folder": mailbox.config["drafts_folder"]}


@router.post("/email/send")
async def send_email(body: EmailCompose, auth: CurrentAuth, db: DB, settings: AppSettings) -> dict:
    """The user sends from the UI (the model's email_send asks the user every time)."""
    mailbox, message = await _compose(body, auth.user.id, db, settings)
    try:
        await mailbox.send(message)
    except EmailError as exc:
        raise _email_error(exc) from exc
    audit(db, auth.user.id, "email.sent", "integration", body.account_id, to=message["To"])
    await db.commit()
    return {"sent": True}


# ---------------------------------------------------------------------- automations


def _automation_out(a: Automation) -> AutomationOut:
    return AutomationOut(
        id=a.id,
        name=a.name,
        enabled=a.enabled,
        trigger=a.trigger,
        conditions=a.conditions,
        actions=a.actions,
        next_check_at=a.next_check_at,
        last_run_at=a.last_run_at,
        last_status=a.last_status,
        consecutive_failures=a.consecutive_failures,
        disabled_reason=a.disabled_reason,
        created_by=a.created_by,
        created_at=a.created_at,
    )


async def _owned_automation(db, owner_id, automation_id) -> Automation:
    found = await db.scalar(
        select(Automation).where(Automation.id == automation_id, Automation.owner_id == owner_id)
    )
    if found is None:
        raise not_found("Automation")
    return found


@router.get("/automations", response_model=list[AutomationOut])
async def list_automations(auth: CurrentAuth, db: DB) -> list[AutomationOut]:
    rows = (
        await db.scalars(
            select(Automation)
            .where(Automation.owner_id == auth.user.id)
            .order_by(Automation.created_at)
        )
    ).all()
    return [_automation_out(a) for a in rows]


@router.post("/automations", response_model=AutomationOut, status_code=status.HTTP_201_CREATED)
async def create_automation(
    body: AutomationCreate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> AutomationOut:
    automation = await automation_svc.create(
        db,
        auth.user.id,
        name=body.name,
        trigger=body.trigger,
        conditions=body.conditions,
        actions=body.actions,
        settings=settings,
    )
    await db.commit()
    return _automation_out(automation)


@router.patch("/automations/{automation_id}", response_model=AutomationOut)
async def update_automation(
    automation_id: uuid.UUID,
    body: AutomationUpdate,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
) -> AutomationOut:
    automation = await _owned_automation(db, auth.user.id, automation_id)
    await automation_svc.update(db, automation, body.model_dump(exclude_none=True), settings)
    await db.commit()
    return _automation_out(automation)


@router.get("/automations/{automation_id}/runs", response_model=list[AutomationRunOut])
async def automation_runs(
    automation_id: uuid.UUID, auth: CurrentAuth, db: DB
) -> list[AutomationRunOut]:
    automation = await _owned_automation(db, auth.user.id, automation_id)
    rows = (
        await db.scalars(
            select(AutomationRun)
            .where(AutomationRun.automation_id == automation.id)
            .order_by(AutomationRun.created_at.desc())
            .limit(100)
        )
    ).all()
    return [
        AutomationRunOut(
            id=r.id,
            status=r.status,
            reason=r.reason,
            detail=r.detail,
            error=r.error,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.delete("/automations/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(automation_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    automation = await _owned_automation(db, auth.user.id, automation_id)
    await db.execute(delete(Automation).where(Automation.id == automation.id))
    audit(db, auth.user.id, "automation.deleted", "automation", automation.id)
    record_sync(db, auth.user.id, "automation", automation.id, operation="delete")
    await db.commit()
