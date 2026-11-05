"""Calendars behind one interface (ADR C-11): Celius's own (local) calendar, always
there, and CalDAV accounts (Nextcloud, Radicale, iCloud, Fastmail, Google via CalDAV…).

Event ids are "local:<uuid>" or "<account uuid>:<href>[#<recurrence-id>]". Recurring
CalDAV events are expanded by the server; occurrences are read-only here (change them in
the calendar app). Every network call goes through the guarded client.
"""

import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.errors import AppError
from app.models import CalendarEvent, IntegrationAccount
from app.services.integrations import accounts
from app.services.integrations.ical import VEvent, parse_events, serialize
from app.services.tools.netguard import BlockedDestination, check_url, guarded_client

# Tests replace this with a fake CalDAV server.
TRANSPORT: httpx.AsyncBaseTransport | None = None
TIMEOUT = 20
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
MAX_EVENTS = 500


class CalendarError(Exception):
    """A calendar server problem, told to the user (and the model) as is."""


@dataclass
class Event:
    id: str
    calendar: str  # "Celius" or the account's name
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str | None = None
    description: str | None = None
    editable: bool = True
    task_id: str | None = None
    extra: dict = field(default_factory=dict)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def overlaps(a: Event, b: Event) -> bool:
    return _aware(a.start) < _aware(b.end) and _aware(b.start) < _aware(a.end)


def conflicts(events: list[Event]) -> list[tuple[Event, Event]]:
    """Pairs of timed events that overlap (all-day events don't block time)."""
    timed = sorted((e for e in events if not e.all_day), key=lambda e: _aware(e.start))
    found = []
    for i, a in enumerate(timed):
        for b in timed[i + 1 :]:
            if _aware(b.start) >= _aware(a.end):
                break
            found.append((a, b))
    return found


def free_slots(
    events: list[Event], start: datetime, end: datetime, minimum: timedelta
) -> list[tuple[datetime, datetime]]:
    busy = sorted(
        ((_aware(e.start), _aware(e.end)) for e in events if not e.all_day), key=lambda p: p[0]
    )
    slots, cursor = [], start
    for b_start, b_end in busy:
        if b_end <= cursor:
            continue
        if b_start > cursor and b_start - cursor >= minimum:
            slots.append((cursor, min(b_start, end)))
        cursor = max(cursor, b_end)
        if cursor >= end:
            break
    if end - cursor >= minimum:
        slots.append((cursor, end))
    return [(a, b) for a, b in slots if b > a]


# ---------------------------------------------------------------------- local


def _local(event: CalendarEvent) -> Event:
    return Event(
        id=f"local:{event.id}",
        calendar="Celius",
        title=event.title,
        start=_aware(event.starts_at),
        end=_aware(event.ends_at),
        all_day=event.all_day,
        location=event.location,
        description=event.description,
        task_id=str(event.task_id) if event.task_id else None,
    )


async def _local_events(
    db: AsyncSession, owner_id: uuid.UUID, start: datetime, end: datetime
) -> list[Event]:
    rows = (
        await db.scalars(
            select(CalendarEvent)
            .where(
                CalendarEvent.owner_id == owner_id,
                and_(CalendarEvent.starts_at < end, CalendarEvent.ends_at > start),
            )
            .order_by(CalendarEvent.starts_at)
            .limit(MAX_EVENTS)
        )
    ).all()
    return [_local(e) for e in rows]


# ---------------------------------------------------------------------- CalDAV


class CalDAV:
    def __init__(self, account: IntegrationAccount, settings: Settings):
        self.account = account
        self.url = account.config["url"]
        secret = accounts.credentials(settings, account)
        auth = (account.config["username"], secret.get("password", "")) if secret else None
        self._client = guarded_client(
            timeout=TIMEOUT,
            allow_private=account.allow_private_network,
            transport=TRANSPORT,
        )
        self._auth = auth

    async def __aenter__(self) -> "CalDAV":
        try:
            check_url(self.url)
        except BlockedDestination as exc:
            await self._client.aclose()
            raise CalendarError(str(exc)) from exc
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            check_url(url)
            response = await self._client.request(method, url, auth=self._auth, **kwargs)
        except BlockedDestination as exc:
            raise CalendarError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise CalendarError(f"Can't reach {urlsplit(url).hostname}: {exc}") from exc
        if response.status_code == 401:
            raise CalendarError(
                f"{self.account.name}: the server refused the user name or password"
            )
        if response.status_code in (301, 302, 307, 308):
            raise CalendarError(
                f"{self.account.name}: the server redirects; use the calendar's full URL "
                f"({response.headers.get('location', 'see its settings')})"
            )
        return response

    async def check(self) -> None:
        response = await self._request("PROPFIND", self.url, headers={"Depth": "0"}, content=b"")
        if response.status_code not in (200, 207):
            raise CalendarError(f"{self.account.name}: HTTP {response.status_code}")

    async def events(self, start: datetime, end: datetime, tz: ZoneInfo) -> list[Event]:
        span = f'start="{start:%Y%m%dT%H%M%SZ}" end="{end:%Y%m%dT%H%M%SZ}"'
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data><c:expand {span}/></c:calendar-data></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range {span}/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""
        response = await self._request(
            "REPORT",
            self.url,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            content=body.encode(),
        )
        if response.status_code != 207:
            raise CalendarError(f"{self.account.name}: HTTP {response.status_code}")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise CalendarError(
                f"{self.account.name}: the server's answer isn't valid XML"
            ) from exc
        found: list[Event] = []
        for item in root.findall("d:response", NS):
            href = item.findtext("d:href", default="", namespaces=NS)
            data = item.findtext(".//c:calendar-data", default="", namespaces=NS)
            vevents = parse_events(data, tz)
            shared = len(vevents) > 1
            for vevent in vevents:
                key = f"{self.account.id}:{href}"
                if vevent.recurrence_id:
                    key += f"#{vevent.recurrence_id}"
                found.append(
                    Event(
                        id=key,
                        calendar=self.account.name,
                        title=vevent.title,
                        start=vevent.start,
                        end=vevent.end,
                        all_day=vevent.all_day,
                        location=vevent.location,
                        description=vevent.description,
                        editable=not (vevent.recurring or shared),
                        extra={"uid": vevent.uid, "href": href},
                    )
                )
        return found[:MAX_EVENTS]

    async def create(self, event: VEvent, tz: ZoneInfo) -> str:
        href = urljoin(self.url, f"{event.uid}.ics")
        response = await self._request(
            "PUT",
            href,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            content=serialize(event, tz).encode(),
        )
        if response.status_code not in (200, 201, 204):
            raise CalendarError(
                f"{self.account.name}: couldn't create the event (HTTP {response.status_code})"
            )
        return urlsplit(href).path

    async def fetch(self, href: str, tz: ZoneInfo) -> tuple[VEvent, str | None]:
        response = await self._request("GET", urljoin(self.url, href))
        if response.status_code != 200:
            raise CalendarError(
                f"{self.account.name}: event not found (HTTP {response.status_code})"
            )
        events = parse_events(response.text, tz)
        if not events:
            raise CalendarError("The event has no data")
        if len(events) > 1 or any(e.recurring for e in events):
            # Rewriting the object would drop the other events in it (a series and its
            # exceptions, or several events in one file).
            raise CalendarError(
                "This event is part of a repeating series (or shares its file with others); "
                "change it in the calendar app"
            )
        return events[0], response.headers.get("etag")

    async def replace(self, href: str, event: VEvent, etag: str | None, tz: ZoneInfo) -> None:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        response = await self._request(
            "PUT", urljoin(self.url, href), headers=headers, content=serialize(event, tz).encode()
        )
        if response.status_code == 412:
            raise CalendarError("The event changed on the server meanwhile; look again")
        if response.status_code not in (200, 201, 204):
            raise CalendarError(f"{self.account.name}: couldn't save (HTTP {response.status_code})")

    async def delete(self, href: str) -> None:
        response = await self._request("DELETE", urljoin(self.url, href))
        if response.status_code not in (200, 204, 404):
            raise CalendarError(
                f"{self.account.name}: couldn't delete (HTTP {response.status_code})"
            )


# ---------------------------------------------------------------------- service


async def list_events(
    db: AsyncSession,
    owner_id: uuid.UUID,
    start: datetime,
    end: datetime,
    settings: Settings,
    tz: ZoneInfo,
) -> tuple[list[Event], list[str]]:
    """Events from every calendar, and the problems of the ones that couldn't be read
    (a calendar that is down doesn't hide the others)."""
    events = await _local_events(db, owner_id, start, end)
    problems: list[str] = []
    for account in await accounts.accounts_of(db, owner_id, "calendar"):
        try:
            async with CalDAV(account, settings) as client:
                events += await client.events(start, end, tz)
            account.last_error, account.last_checked_at = None, utcnow()
        except (CalendarError, AppError) as exc:
            message = getattr(exc, "message", None) or str(exc)
            account.last_error, account.last_checked_at = message[:500], utcnow()
            problems.append(message)
    events.sort(key=lambda e: (_aware(e.start), e.title))
    return events, problems


async def create_event(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    title: str,
    start: datetime,
    end: datetime,
    all_day: bool,
    settings: Settings,
    tz: ZoneInfo,
    calendar: str | None = None,  # an account id, or None/"local"
    location: str | None = None,
    description: str | None = None,
    task_id: uuid.UUID | None = None,
) -> Event:
    if _aware(end) <= _aware(start) and not all_day:
        raise AppError(422, "invalid_time", "The event must end after it starts")
    if all_day and _aware(end) <= _aware(start):
        end = start + timedelta(days=1)
    title = " ".join(title.split())[:300] or "(no title)"
    if not calendar or calendar == "local":
        row = CalendarEvent(
            id=uuid.uuid4(),
            owner_id=owner_id,
            title=title,
            starts_at=start,
            ends_at=end,
            all_day=all_day,
            location=location,
            description=description,
            task_id=task_id,
        )
        db.add(row)
        await db.flush()
        return _local(row)
    account = await accounts.owned(db, owner_id, calendar, kind="calendar")
    event = VEvent(
        uid=f"{uuid.uuid4()}@celius",
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        location=location,
        description=description,
    )
    try:
        async with CalDAV(account, settings) as client:
            href = await client.create(event, tz)
    except CalendarError as exc:
        raise AppError(502, "calendar_error", str(exc)) from exc
    return Event(
        id=f"{account.id}:{href}",
        calendar=account.name,
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        location=location,
        description=description,
    )


def _split_id(event_id: str) -> tuple[str, str, str | None]:
    source, _, rest = event_id.partition(":")
    href, _, recurrence = rest.partition("#")
    return source, href, recurrence or None


async def update_event(
    db: AsyncSession,
    owner_id: uuid.UUID,
    event_id: str,
    changes: dict,
    settings: Settings,
    tz: ZoneInfo,
) -> None:
    source, ref, recurrence = _split_id(event_id)
    if source == "local":
        row = await db.scalar(
            select(CalendarEvent).where(
                CalendarEvent.id == uuid.UUID(ref), CalendarEvent.owner_id == owner_id
            )
        )
        if row is None:
            raise AppError(404, "not_found", "Event not found")
        for key, column in (
            ("title", "title"),
            ("start", "starts_at"),
            ("end", "ends_at"),
            ("location", "location"),
            ("description", "description"),
            ("all_day", "all_day"),
        ):
            if key in changes:
                setattr(row, column, changes[key])
        if _aware(row.ends_at) <= _aware(row.starts_at):
            raise AppError(422, "invalid_time", "The event must end after it starts")
        return
    if recurrence:
        raise AppError(
            409, "recurring_event", "Repeating events can't be changed here; use the calendar app"
        )
    account = await accounts.owned(db, owner_id, source, kind="calendar")
    try:
        async with CalDAV(account, settings) as client:
            current, etag = await client.fetch(ref, tz)
            if current.recurring:
                raise CalendarError("Repeating events can't be changed here; use the calendar app")
            for key in ("title", "start", "end", "location", "description", "all_day"):
                if key in changes:
                    setattr(current, key, changes[key])
            if current.end <= current.start and not current.all_day:
                raise CalendarError("The event must end after it starts")
            await client.replace(ref, current, etag, tz)
    except CalendarError as exc:
        raise AppError(502, "calendar_error", str(exc)) from exc


async def delete_event(
    db: AsyncSession, owner_id: uuid.UUID, event_id: str, settings: Settings
) -> None:
    source, ref, recurrence = _split_id(event_id)
    if source == "local":
        row = await db.scalar(
            select(CalendarEvent).where(
                CalendarEvent.id == uuid.UUID(ref), CalendarEvent.owner_id == owner_id
            )
        )
        if row is None:
            raise AppError(404, "not_found", "Event not found")
        await db.delete(row)
        return
    if recurrence:
        raise AppError(
            409, "recurring_event", "Repeating events can't be deleted here; use the calendar app"
        )
    account = await accounts.owned(db, owner_id, source, kind="calendar")
    try:
        async with CalDAV(account, settings) as client:
            await client.fetch(ref, ZoneInfo("UTC"))  # refuses series and shared files
            await client.delete(ref)
    except CalendarError as exc:
        raise AppError(502, "calendar_error", str(exc)) from exc


def describe(event: Event, tz: ZoneInfo) -> str:
    start = _aware(event.start).astimezone(tz)
    end = _aware(event.end).astimezone(tz)
    if event.all_day:
        when = f"{start:%Y-%m-%d} (all day)"
    elif start.date() == end.date():
        when = f"{start:%Y-%m-%d %H:%M}–{end:%H:%M}"
    else:
        when = f"{start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M}"
    where = f", {event.location}" if event.location else ""
    calendar = "" if event.calendar == "Celius" else f" [{event.calendar}]"
    return f"{when} {event.title}{where}{calendar} (id {event.id})"
