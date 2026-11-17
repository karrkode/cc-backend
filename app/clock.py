"""CC-276: the one clock. Every "today" in the app is Chicago's day, and every timestamp is UTC.

Tests freeze time with the `frozen_clock` fixture (tests/conftest.py), which swaps `utcnow` here and in every
module that imported it. Don't call `date.today()` or `datetime.now()` anywhere else.
"""
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """A stored time with its zone put back: SQLite hands back naive datetimes, which are UTC."""
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def chicago_day(ts: datetime) -> date:
    return as_utc(ts).astimezone(CHICAGO).date()


def today_in_chicago() -> date:
    return utcnow().astimezone(CHICAGO).date()
