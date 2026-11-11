"""CC-170: "Add to calendar": an event on the day you're going, as a calendar file (.ics, RFC 5545).

Plain iCalendar that iPhone, Google Calendar and Outlook all open: times are written in UTC (no VTIMEZONE block,
which Outlook is picky about), text is escaped and lines are folded at 75 octets."""
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .addresses import full_address
from .config import get_settings
from .models import Event
from .seo import slug

CHICAGO = ZoneInfo("America/Chicago")
LENGTH = timedelta(hours=2)  # no event says how long it runs; two hours is a fair guess for a show


def _text(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Lines longer than 75 octets go on in continuation lines that start with a space, never splitting a character."""
    out, chunk, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > (75 if not out else 74):
            out.append(chunk)
            chunk, size = "", 0
        chunk += ch
        size += n
    out.append(chunk)
    return "\r\n ".join(out)


def _utc(d: date, hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), CHICAGO).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


GALLERY = "Visual Art"
DEFAULT_TIME = "13:00"  # CC-170: the user's choice when the event lists no time that day
UNSURE = "We couldn't find a time for the event so please verify!"  # said on a show given DEFAULT_TIME


def _own_times(event: Event, d: date) -> list[str]:
    """The times the event itself lists for day d: a fixed run's date, a found performance, or its own weekly pattern."""
    from .roundups import WEEKDAYS
    times = [r.time for r in event.run_dates if r.date == d and r.time]
    times += [p.get("time") or "" for p in event.performances or [] if p.get("date") == d.isoformat()]
    return times or list((event.weekly or {}).get(WEEKDAYS[d.weekday()]) or [])


def start_time(event: Event, d: date) -> tuple[str, bool]:
    """The first start time it has on day d ("19:30"), and whether that time is only a guess. With no time listed it's
    1 pm, which is only a guess for a show; for a gallery's exhibition it's simply when to go. An exhibition uses only
    times it lists itself (the venue's show times are for its other events)."""
    from .discovery.store import _hhmm
    from .roundups import times_on  # here: roundups imports the events router, which serves this file
    gallery = event.category == GALLERY
    listed = _own_times(event, d) if gallery else times_on(event, d) or []
    times = sorted(t for t in map(_hhmm, listed) if t)
    return (times[0], False) if times else (DEFAULT_TIME, not gallery)


def filename(event: Event) -> str:
    return f"{slug(event.title)}.ics"


def build(event: Event, d: date, now: datetime | None = None, at: str | None = None) -> str:
    """at: the show you chose that day (CC-264, "16:00"); without it, the day's first show (start_time)."""
    venue = event.venue
    link = f"{get_settings().site_url}/#e/{event.id}"  # the event's view in the site, as in emails (CC-117)
    where = ", ".join(p for p in (venue.name, full_address(venue.address, venue.city, venue.zip_code, venue.area,
                                                                       venue.neighborhood)) if p)
    begins, guessed = (at, False) if at else start_time(event, d)
    h, m = (int(x) for x in begins.split(":"))
    ends = (datetime.combine(d, time(h, m)) + LENGTH).strftime("%H:%M")
    end_day = d if ends > begins else d + timedelta(days=1)  # a late show running past midnight
    about = f"{event.title} at {venue.name}. Event page: {link}"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Culture Crew//Add to calendar//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:cc-{event.id}-{d:%Y%m%d}@chiculturecrew.com",
        f"DTSTAMP:{(now or datetime.now(UTC)).astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"DTSTART:{_utc(d, begins)}", f"DTEND:{_utc(end_day, ends)}",
        f"SUMMARY:{_text(event.title)}",
        f"LOCATION:{_text(where)}",
        f"DESCRIPTION:{_text(f'{UNSURE}\n\n{about}' if guessed else about)}",
        f"URL:{link}",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(map(_fold, lines)) + "\r\n"
