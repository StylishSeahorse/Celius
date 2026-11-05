"""The subset of iCalendar (RFC 5545) Celius needs for CalDAV: VEVENTs with start, end
or duration, summary, location, description, UID and RECURRENCE-ID; dates in UTC, with a
TZID, floating, or all-day. Recurring events are expanded by the CalDAV server (calendar-
query with <expand>), so RRULE is never evaluated here.

No dependency: the format is line-based, and a full library would bring far more than
this needs (C-11).
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DURATION = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


@dataclass
class VEvent:
    uid: str
    title: str
    start: datetime  # UTC
    end: datetime  # UTC
    all_day: bool = False
    location: str | None = None
    description: str | None = None
    recurrence_id: str | None = None
    recurring: bool = False


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split(line: str) -> tuple[str, dict[str, str], str]:
    """NAME;PARAM=VALUE;...:value (a colon inside a quoted parameter doesn't end it)."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            head, value = line[:index], line[index + 1 :]
            break
    else:
        return line.upper(), {}, ""
    name, *params = head.split(";")
    parsed = {}
    for param in params:
        key, _, val = param.partition("=")
        parsed[key.upper()] = val.strip('"')
    return name.upper(), parsed, value


def unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _zone(name: str | None, default: ZoneInfo) -> ZoneInfo:
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return default


def parse_datetime(
    value: str, params: dict[str, str], default_zone: ZoneInfo
) -> tuple[datetime, bool]:
    """(UTC datetime, all day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(day, time(0, 0), default_zone).astimezone(UTC), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), False
    local = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    return local.replace(tzinfo=_zone(params.get("TZID"), default_zone)).astimezone(UTC), False


def parse_duration(value: str) -> timedelta:
    match = _DURATION.match(value.strip())
    if not match:
        return timedelta(0)
    parts = {k: int(v) for k, v in match.groupdict().items() if v and k != "sign"}
    delta = timedelta(
        weeks=parts.get("weeks", 0),
        days=parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )
    return -delta if match.group("sign") == "-" else delta


def parse_events(text: str, default_zone: ZoneInfo | None = None) -> list[VEvent]:
    """Every VEVENT in an iCalendar text (nested VALARMs and VTIMEZONEs are skipped)."""
    default_zone = default_zone or ZoneInfo("UTC")
    events: list[VEvent] = []
    current: dict | None = None
    depth = 0  # components inside the VEVENT (VALARM)
    for line in unfold(text):
        name, params, value = _split(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            current, depth = {}, 0
            continue
        if current is None:
            continue
        if name == "BEGIN":
            depth += 1
            continue
        if name == "END" and depth:
            depth -= 1
            continue
        if name == "END" and value.upper() == "VEVENT":
            if "DTSTART" in current:
                start, all_day = current["DTSTART"]
                if "DTEND" in current:
                    end = current["DTEND"][0]
                elif "DURATION" in current:
                    end = start + current["DURATION"]
                else:
                    end = start + (timedelta(days=1) if all_day else timedelta(0))
                events.append(
                    VEvent(
                        uid=current.get("UID") or str(uuid.uuid4()),
                        title=current.get("SUMMARY") or "(no title)",
                        start=start,
                        end=max(end, start),
                        all_day=all_day,
                        location=current.get("LOCATION"),
                        description=current.get("DESCRIPTION"),
                        recurrence_id=current.get("RECURRENCE-ID"),
                        recurring="RRULE" in current or "RECURRENCE-ID" in current,
                    )
                )
            current = None
            continue
        if depth:
            continue
        if name in ("DTSTART", "DTEND"):
            current[name] = parse_datetime(value, params, default_zone)
        elif name == "DURATION":
            current[name] = parse_duration(value)
        elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID"):
            current[name] = unescape(value)
        elif name in ("RECURRENCE-ID", "RRULE"):
            current[name] = value
    return events


def _fold(line: str) -> str:
    out, data = [], line.encode()
    while len(data) > 75:
        cut = 75 if not out else 74
        while cut > 0 and (data[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(data[:cut].decode())
        data = data[cut:]
    out.append(data.decode())
    return "\r\n ".join(out)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def serialize(event: VEvent, local_zone: ZoneInfo | None = None) -> str:
    """A VCALENDAR with one VEVENT. All-day dates are written as DATEs in `local_zone`."""
    local_zone = local_zone or ZoneInfo("UTC")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Celius//Celius//EN",
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{_stamp(datetime.now(UTC))}",
    ]
    if event.all_day:
        first: date = event.start.astimezone(local_zone).date()
        last: date = event.end.astimezone(local_zone).date()
        if last <= first:
            last = first + timedelta(days=1)
        lines.append(f"DTSTART;VALUE=DATE:{first:%Y%m%d}")
        lines.append(f"DTEND;VALUE=DATE:{last:%Y%m%d}")
    else:
        lines.append(f"DTSTART:{_stamp(event.start)}")
        lines.append(f"DTEND:{_stamp(event.end)}")
    lines.append(f"SUMMARY:{escape(event.title)}")
    if event.location:
        lines.append(f"LOCATION:{escape(event.location)}")
    if event.description:
        lines.append(f"DESCRIPTION:{escape(event.description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
