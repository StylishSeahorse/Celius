"""The event finder's scans, suggestions and settings (ADR C-17).

A scan reads each enabled source's new posts (newer than the last one read), asks the
model for the events in each, and saves them as suggestions. The same event announced
by several sources (venue, band, promoter) becomes one suggestion. Nothing reaches a
calendar until the user approves it; then it goes to the calendar they chose (Nextcloud
through CalDAV, or Celius's own).
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import (
    EventSource,
    EventSuggestion,
    IntegrationAccount,
    Job,
    JobStatus,
    UserSettings,
)
from app.services import model_registry, push
from app.services.assistant import tasks as assistant_tasks
from app.services.events import extract, facebook
from app.services.integrations import accounts, meta
from app.services.integrations import calendar as cal
from app.services.jobs import enqueue_job, job_handler
from app.services.model_gateway import ModelGatewayError

log = logging.getLogger(__name__)

FIRST_SCAN_DAYS = 21  # how far back the first scan of a new source reads
MAX_SOURCES = 100
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "at", "with", "live", "night", "presents", "feat", "ft", "in"}


# ---------------------------------------------------------------------- settings


@dataclass
class EventPrefs:
    every_days: int
    calendar: str | None  # an account id, "local", or None (ask)
    scanned_at: datetime | None


def _section(row: UserSettings | None) -> dict:
    return dict(((row.preferences if row else None) or {}).get("events") or {})


async def prefs(db: AsyncSession, owner_id: uuid.UUID, settings: Settings) -> EventPrefs:
    saved = _section(await db.get(UserSettings, owner_id))
    scanned = saved.get("scanned_at")
    return EventPrefs(
        every_days=int(saved.get("every_days") or settings.event_scan_days),
        calendar=saved.get("calendar"),
        scanned_at=datetime.fromisoformat(scanned) if scanned else None,
    )


async def save_prefs(db: AsyncSession, owner_id: uuid.UUID, **changes: Any) -> None:
    row = await db.get(UserSettings, owner_id)
    if row is None:
        row = UserSettings(user_id=owner_id, preferences={})
        db.add(row)
    section = {**_section(row), **changes}
    row.preferences = {**(row.preferences or {}), "events": section}


async def meta_account(db: AsyncSession, owner_id: uuid.UUID) -> IntegrationAccount | None:
    found = await accounts.accounts_of(db, owner_id, "social")
    return next((a for a in found if a.provider == "meta"), None)


# ---------------------------------------------------------------------- sources


async def add_source(
    db: AsyncSession, owner_id: uuid.UUID, platform: str, handle: str, label: str | None
) -> EventSource:
    if platform == "instagram":
        handle = handle.strip().lstrip("@").rstrip("/").split("/")[-1]
        if not meta.USERNAME.fullmatch(handle):
            raise AppError(422, "invalid_handle", "Use the account's username, like @yourvenue")
    elif platform == "facebook":
        try:
            handle = facebook.clean_page_url(handle)
        except facebook.FacebookError as exc:
            raise AppError(422, "invalid_handle", str(exc)) from exc
    else:
        raise AppError(422, "invalid_platform", "Instagram or Facebook")
    existing = (await db.scalars(select(EventSource).where(EventSource.owner_id == owner_id))).all()
    if any(s.platform == platform and s.handle.lower() == handle.lower() for s in existing):
        raise AppError(409, "duplicate", "That one is already on the list")
    if len(existing) >= MAX_SOURCES:
        raise AppError(409, "too_many", f"Up to {MAX_SOURCES} sources")
    source = EventSource(
        id=uuid.uuid4(),
        owner_id=owner_id,
        platform=platform,
        handle=handle,
        label=(label or "").strip()[:200] or None,
        enabled=True,
    )
    db.add(source)
    await db.flush()
    return source


async def owned_source(db: AsyncSession, owner_id: uuid.UUID, source_id) -> EventSource:
    source = await db.scalar(
        select(EventSource).where(EventSource.id == source_id, EventSource.owner_id == owner_id)
    )
    if source is None:
        raise not_found("Source")
    return source


async def owned_suggestion(db: AsyncSession, owner_id: uuid.UUID, suggestion_id) -> EventSuggestion:
    row = await db.scalar(
        select(EventSuggestion).where(
            EventSuggestion.id == suggestion_id, EventSuggestion.owner_id == owner_id
        )
    )
    if row is None:
        raise not_found("Event")
    return row


# ---------------------------------------------------------------------- de-duplication


def _words(text: str | None) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def dedup_key(found: extract.Found, tz) -> str:
    day = found.starts_at.astimezone(tz).date().isoformat()
    title = " ".join(sorted(_words(found.title)))[:200]
    return f"{day}|{title}"


def same_event(found: extract.Found, row: EventSuggestion, tz) -> bool:
    """Announced by two sources: same day, and similar titles (or one inside the other),
    or the same venue and start time."""
    if found.starts_at.astimezone(tz).date() != row.starts_at.astimezone(tz).date():
        return False
    a, b = _words(found.title), _words(row.title)
    # Similar, or one title inside the other ("Jazz Night" / "Jazz Night with the Trio").
    if a and b and (len(a & b) / len(a | b) >= 0.5 or a <= b or b <= a):
        return True
    return bool(
        found.venue
        and row.venue
        and _words(found.venue) == _words(row.venue)
        and not found.all_day
        and not row.all_day
        and found.starts_at == row.starts_at
    )


async def store(
    db: AsyncSession,
    owner_id: uuid.UUID,
    source: EventSource,
    post: dict,
    events: list[extract.Found],
    tz,
) -> int:
    """Save new suggestions; a known event gets the post added to it. Returns how many
    are new."""
    new = 0
    for found in events:
        day_start = found.starts_at - timedelta(days=1)
        nearby = (
            await db.scalars(
                select(EventSuggestion).where(
                    EventSuggestion.owner_id == owner_id,
                    EventSuggestion.starts_at >= day_start,
                    EventSuggestion.starts_at <= found.starts_at + timedelta(days=1),
                )
            )
        ).all()
        match = next((row for row in nearby if same_event(found, row, tz)), None)
        if match is not None:
            if post.get("url") and post["url"] not in [match.post_url, *match.also_posted]:
                match.also_posted = [*match.also_posted, post["url"]][:20]
            for field in ("venue", "address", "price", "ticket_url", "summary"):
                if getattr(match, field) is None and getattr(found, field):
                    setattr(match, field, getattr(found, field))
            continue
        suggestion = EventSuggestion(
            id=uuid.uuid4(),
            owner_id=owner_id,
            source_id=source.id,
            status="pending",
            title=found.title,
            starts_at=found.starts_at,
            ends_at=found.ends_at,
            all_day=found.all_day,
            venue=found.venue or (source.label if source.label else None),
            address=found.address,
            price=found.price,
            summary=found.summary,
            ticket_url=found.ticket_url,
            post_url=post.get("url"),
            image_url=(post.get("images") or [None])[0],
            posted_at=post.get("posted_at"),
            also_posted=[],
            dedup_key=dedup_key(found, tz),
        )
        try:
            async with db.begin_nested():  # a rolled-back savepoint drops the new row
                db.add(suggestion)
                await db.flush()
        except IntegrityError:  # the same key from another source: already suggested
            log.info("Event %r already suggested", found.title)
            continue
        new += 1
    return new


# ---------------------------------------------------------------------- scanning


async def _posts(settings: Settings, source: EventSource, account, creds, limit: int):
    if source.platform == "instagram":
        if account is None:
            raise meta.MetaError("Sign in with Meta to read Instagram accounts")
        return await meta.instagram_posts(settings, account.config, creds, source.handle, limit)
    return await facebook.page_posts(settings, source.handle, source.seen_until, limit)


async def scan(factory: SessionFactory, settings: Settings, owner_id: uuid.UUID, gateway) -> dict:
    async with factory() as db:
        sources = (
            await db.scalars(
                select(EventSource)
                .where(EventSource.owner_id == owner_id, EventSource.enabled.is_(True))
                .order_by(EventSource.created_at)
            )
        ).all()
        account = await meta_account(db, owner_id)
        creds = accounts.credentials(settings, account) if account else {}
        assistant = await assistant_tasks.user_prefs(db, owner_id, settings)
        tz = assistant_tasks.zone(assistant.timezone)
        row = await db.get(UserSettings, owner_id)
        model_id = (row.default_model_id if row else None) or settings.model_default_id
        vision = structured = False
        try:
            async with db.begin_nested():
                profile = await model_registry.resolve(db, gateway, model_id, settings)
            vision, structured = profile.vision, profile.structured_output
        except ModelGatewayError:
            pass
        await save_prefs(db, owner_id, scanned_at=utcnow().isoformat())
        await db.commit()

    today = utcnow().astimezone(tz).date()
    budget = settings.event_max_posts_per_scan
    totals = {"sources": 0, "posts": 0, "found": 0, "errors": 0}
    for source in sources:
        if budget <= 0:
            break
        error = None
        newest = source.seen_until
        try:
            posts = await _posts(settings, source, account, creds, settings.event_posts_per_source)
        except (meta.MetaError, facebook.FacebookError) as exc:
            posts, error = [], str(exc)
            totals["errors"] += 1
        cutoff = source.seen_until or utcnow() - timedelta(days=FIRST_SCAN_DAYS)
        fresh = [p for p in posts if p["posted_at"] is None or p["posted_at"] > cutoff]
        fresh = fresh[:budget]
        budget -= len(fresh)
        found = 0
        for post in fresh:
            images = []
            if vision:
                for url in post.get("images") or []:
                    if uri := await extract.image_data(url):
                        images.append(uri)
            request = extract.build_request(
                model_id, post, today, tz, images, structured=structured
            )
            events = extract.parse(await extract.ask(gateway, request), today, tz)
            async with factory() as db:
                fresh_source = await db.get(EventSource, source.id)
                if fresh_source is None:
                    break
                found += await store(db, owner_id, fresh_source, post, events, tz)
                await db.commit()
            if post["posted_at"] and (newest is None or post["posted_at"] > newest):
                newest = post["posted_at"]
        async with factory() as db:
            row = await db.get(EventSource, source.id)
            if row is not None:
                row.last_checked_at = utcnow()
                row.last_error = error[:500] if error else None
                if newest:
                    row.seen_until = newest
            await db.commit()
        totals["sources"] += 1
        totals["posts"] += len(fresh)
        totals["found"] += found
    if totals["found"]:
        async with factory() as db:
            push.queue(
                db,
                owner_id,
                title="Events found",
                body=f"{totals['found']} new event{'s' if totals['found'] != 1 else ''} to look at",
                url="/#/today?tab=events",
                tag="events",
            )
            await db.commit()
    return totals


@job_handler("events.scan")
async def scan_job(factory: SessionFactory, settings: Settings, job: Job, gateway=None) -> dict:
    if gateway is None:
        return {"skipped": "no model"}
    return await scan(factory, settings, job.owner_id, gateway)


async def scanning(db: AsyncSession, owner_id: uuid.UUID) -> bool:
    return (
        await db.scalar(
            select(Job.id).where(
                Job.owner_id == owner_id,
                Job.kind == "events.scan",
                or_(Job.status == JobStatus.queued, Job.status == JobStatus.running),
            )
        )
    ) is not None


async def queue_scan(db: AsyncSession, owner_id: uuid.UUID) -> bool:
    if await scanning(db, owner_id):
        return False
    enqueue_job(db, owner_id, "events.scan", {})
    return True


async def queue_due(factory: SessionFactory, settings: Settings) -> int:
    """From the worker's maintenance: a scan for each user with sources whose last scan
    is older than their interval (every 4 days by default)."""
    queued = 0
    async with factory() as db:
        owners = (
            await db.scalars(
                select(EventSource.owner_id).where(EventSource.enabled.is_(True)).distinct()
            )
        ).all()
        now = utcnow()
        for owner_id in owners:
            saved = await prefs(db, owner_id, settings)
            if saved.scanned_at and saved.scanned_at.replace(
                tzinfo=saved.scanned_at.tzinfo or UTC
            ) > now - timedelta(days=saved.every_days):
                continue
            queued += int(await queue_scan(db, owner_id))
        await db.commit()
    return queued


# ---------------------------------------------------------------------- approving


def description(row: EventSuggestion) -> str:
    lines = [row.summary or ""]
    if row.price:
        lines.append(f"Price: {row.price}")
    if row.ticket_url:
        lines.append(f"Tickets: {row.ticket_url}")
    if row.post_url:
        lines.append(f"Found in: {row.post_url}")
    lines += [f"Also posted: {url}" for url in row.also_posted]
    return "\n".join(line for line in lines if line).strip()


async def add_to_calendar(
    db: AsyncSession,
    owner_id: uuid.UUID,
    row: EventSuggestion,
    settings: Settings,
    calendar: str | None,
) -> cal.Event:
    if row.status == "added":
        raise AppError(409, "already_added", "This event is already in your calendar")
    saved = await prefs(db, owner_id, settings)
    target = calendar or saved.calendar or "local"
    assistant = await assistant_tasks.user_prefs(db, owner_id, settings)
    tz = assistant_tasks.zone(assistant.timezone)
    end = row.ends_at or (
        row.starts_at + timedelta(days=1) if row.all_day else row.starts_at + timedelta(hours=2)
    )
    where = ", ".join(x for x in (row.venue, row.address) if x) or None
    event = await cal.create_event(
        db,
        owner_id,
        title=row.title,
        start=row.starts_at,
        end=end,
        all_day=row.all_day,
        settings=settings,
        tz=tz,
        calendar=target,
        location=where,
        description=description(row),
    )
    row.status = "added"
    row.calendar_event_id = event.id[:1000]
    row.decided_at = utcnow()
    if calendar and calendar != saved.calendar:
        await save_prefs(db, owner_id, calendar=calendar)  # remembered for next time
    return event
