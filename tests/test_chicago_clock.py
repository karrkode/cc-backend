"""CC-276: one Chicago clock. "Today" is Chicago's day at any hour, in winter and in daylight time."""
from datetime import UTC, date, datetime, timedelta

import pytest

from app import clock
from app.clock import as_utc, chicago_day, today_in_chicago
from app.models import EventStatus

from .conftest import auth

# (UTC instant, Chicago date it should be)
MOMENTS = {
    "7 pm Chicago (winter), already tomorrow in UTC": (datetime(2026, 11, 17, 1, 0, tzinfo=UTC), date(2026, 11, 16)),
    "just after midnight Chicago (winter)": (datetime(2026, 11, 17, 6, 5, tzinfo=UTC), date(2026, 11, 17)),
    "a Sunday evening in summer (daylight time)": (datetime(2026, 7, 6, 1, 30, tzinfo=UTC), date(2026, 7, 5)),
    "just after midnight Chicago (summer)": (datetime(2026, 7, 6, 5, 5, tzinfo=UTC), date(2026, 7, 6)),
}


@pytest.mark.parametrize("at,expected", MOMENTS.values(), ids=MOMENTS.keys())
def test_today_is_chicagos_day(frozen_clock, at, expected):
    frozen_clock(at)
    assert today_in_chicago() == expected
    assert chicago_day(at) == expected


def test_as_utc_puts_the_zone_back_on_naive_times():
    naive = datetime(2026, 1, 1, 12, 0)
    assert as_utc(naive).tzinfo is UTC and as_utc(naive).hour == 12
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert as_utc(aware) is aware and as_utc(None) is None
    assert chicago_day(naive) == date(2026, 1, 1)


def test_one_clock_and_one_zone():
    from app import icsfile, stats
    assert stats.CHICAGO is clock.CHICAGO and icsfile.CHICAGO is clock.CHICAGO
    assert stats.today_in_chicago is clock.today_in_chicago


@pytest.mark.parametrize("at,today", MOMENTS.values(), ids=MOMENTS.keys())
def test_an_event_ending_today_stays_in_the_list(client, db, make_event, frozen_clock, at, today):
    """The evening bug: at 7 pm Chicago the server's UTC date is tomorrow, which hid an event ending today."""
    frozen_clock(at)
    make_event(title="Ends tonight", begin=today - timedelta(days=3), end=today)
    make_event(title="Ended yesterday", begin=today - timedelta(days=9), end=today - timedelta(days=1))
    titles = [e["title"] for e in client.get("/events").json()["items"]]
    assert "Ends tonight" in titles and "Ended yesterday" not in titles


def test_admin_stats_use_the_servers_day_not_the_callers(client, admin_token, frozen_clock):
    frozen_clock(datetime(2026, 11, 17, 1, 0, tzinfo=UTC))  # Nov 16 in Chicago
    far = client.get("/admin/stats?weeks=1&today=2030-01-01", headers=auth(admin_token))
    plain = client.get("/admin/stats?weeks=1", headers=auth(admin_token))
    assert far.status_code == plain.status_code == 200
    assert far.json() == plain.json()
    assert plain.json()["weeks"][-1]["start"] == "2026-11-16"  # the Monday of that week
