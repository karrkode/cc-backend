"""CC-240: GET /console/weekly gives the Growth Console one finished week's counts, behind its own key."""
from datetime import UTC, date, datetime, timedelta

import pytest

from app import consolestats, keys
from app.clock import as_utc
from app.config import get_settings
from app.models import (AppSetting, Crew, CrewInvite, CrewMember, Plan, PlanShare, PlanStatus, SentEmail,
                        TrafficCount, TrafficVisitorDay, User)
from app.routers import console as console_router

from .conftest import auth, register

KEY = "k" * 40
WEEK = "2026-11-09"  # Monday. In Chicago it's CST (UTC-6) all week: DST ended on Nov 1.
MON_START = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)   # Monday 00:00 Chicago
NEXT_MON = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)   # the next Monday 00:00 Chicago


@pytest.fixture(autouse=True)
def setup(monkeypatch, frozen_clock):
    keys.reset()
    console_router.limiter.reset()
    monkeypatch.setattr(get_settings(), "console_key", KEY)
    frozen_clock(datetime(2026, 11, 18, 15, 0, tzinfo=UTC))  # Wednesday
    yield
    keys.reset()
    console_router.limiter.reset()


def get(client, week=WEEK, key=KEY):
    return client.get("/console/weekly", params={"week": week} if week else None,
                      headers={"X-Console-Key": key} if key is not None else {})


def user(db, n, at):
    u = User(email=f"u{n}@example.com", password_hash="x", created_at=at)
    db.add(u)
    db.flush()
    return u


def test_key_is_required_and_nothing_is_said_about_why(client):
    for r in (get(client, key=None), get(client, key="wrong"), get(client, key=KEY[:-1]), get(client, key=KEY + "x")):
        assert r.status_code == 401 and r.json() == {"detail": "Unauthorized"} and r.headers["cache-control"] == "no-store"
    assert get(client, week="not-a-date", key="wrong").status_code == 401  # a bad week isn't answered before the key


def test_no_key_set_refuses_everyone(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "console_key", "")
    assert get(client, key="").status_code == 401 and get(client, key=KEY).status_code == 401


def test_sign_in_is_not_a_key_and_the_key_opens_nothing_else(client):
    h = auth(register(client))
    assert client.get("/console/weekly", params={"week": WEEK}, headers=h).status_code == 401
    assert client.get("/admin/stats", headers={"X-Console-Key": KEY}).status_code == 401
    assert client.get("/me/plans", headers={"X-Console-Key": KEY}).status_code == 401


def test_rate_limited_right_or_wrong(client):
    codes = [get(client, key="wrong" if i % 2 else KEY).status_code for i in range(console_router.PER_MINUTE + 2)]
    assert codes[:console_router.PER_MINUTE] == [200 if i % 2 == 0 else 401 for i in range(console_router.PER_MINUTE)]
    assert codes[-2:] == [429, 429]
    assert get(client).json() == {"detail": "Too Many Requests"}


def test_unfinished_or_odd_weeks_are_refused(client):
    assert get(client, week="2026-11-16").status_code == 400   # ends on the 22nd: still running on the 18th
    assert get(client, week="2026-11-10").status_code == 400   # a Tuesday
    assert get(client, week="2027-01-04").status_code == 400   # the future
    assert get(client, week="nope").status_code == 400
    assert get(client, week="9999-12-27").status_code == 400  # not a 500
    assert get(client, week="2026-11-09").status_code == 200


def test_the_default_week_is_the_last_finished_one(client):
    assert get(client, week=None).json()["week_start"] == "2026-11-09"


def test_the_week_is_chicago_time_at_both_edges(client, db):
    one = timedelta(seconds=1)  # the second before Monday 00:00 Chicago closes the week before and this one
    for i, at in enumerate([MON_START - one, MON_START, NEXT_MON - one, NEXT_MON]):
        user(db, i, at)
    db.commit()
    assert get(client).json()["new_members"] == 2  # Monday 00:00:00 and Sunday 23:59:59 only


def test_dst_weeks_are_not_168_hours():
    start, end = consolestats._bounds(date(2026, 10, 26))  # the clocks go back on Sunday Nov 1
    assert (end - start).total_seconds() == 169 * 3600
    start, end = consolestats._bounds(date(2026, 3, 2))    # and forward on Sunday Mar 8, 2026... the week of Mar 2
    assert (end - start).total_seconds() == 167 * 3600


def test_an_empty_week_is_zeros_and_what_cant_be_counted_is_null(client, db):
    db.add(TrafficCount(day=date(2026, 11, 1), kind="view", name="home", count=1))
    db.commit()
    r = get(client).json()
    assert r == {"week_start": WEEK, "week_end": "2026-11-15", "site_visits": 0, "new_members": 0, "event_saves": 0,
                 "crew_invites": 0, "crew_invite_accepts": 0, "crew_suggestions": None}  # no shares dated yet


def test_visits_count_visitor_days_and_are_null_before_the_counter_started(client, db):
    assert get(client).json()["site_visits"] is None  # nothing counted yet
    db.add(TrafficCount(day=date(2026, 11, 10), kind="view", name="home", count=9))
    for day, v in [(date(2026, 11, 8), "a"), (date(2026, 11, 9), "a"), (date(2026, 11, 9), "b"),
                   (date(2026, 11, 15), "a"), (date(2026, 11, 16), "a")]:
        db.add(TrafficVisitorDay(day=day, visitor=v))
    db.commit()
    assert get(client).json()["site_visits"] is None  # the counter's first day (the 10th) is after this week began
    db.add(TrafficCount(day=date(2026, 11, 2), kind="view", name="home", count=1))
    db.commit()
    assert get(client).json()["site_visits"] == 3  # a and b on Monday, a on Sunday


def test_saves_invites_joins_and_shares(client, db, make_event):
    owner, ben, cy = (user(db, i, MON_START) for i in range(3))
    ev = make_event()
    crew = Crew(name="C", owner_id=owner.id)
    db.add(crew)
    db.flush()
    inside, outside = datetime(2026, 11, 12, 18, 0, tzinfo=UTC), datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    db.add_all([
        Plan(user_id=ben.id, event_id=ev.id, status=PlanStatus.interested, created_at=inside),
        Plan(user_id=cy.id, event_id=ev.id, status=PlanStatus.interested, created_at=outside),
        CrewInvite(crew_id=crew.id, token_hash="a" * 64, created_at=inside, expires_at=outside),
        CrewInvite(crew_id=crew.id, token_hash="b" * 64, created_at=outside, expires_at=outside),
        SentEmail(email="x@example.com", kind="crew_invite", sent_at=inside),
        SentEmail(email="x@example.com", kind="crew_invite", sent_at=inside),
        SentEmail(email="x@example.com", kind="verify", sent_at=inside),  # not an invite
        CrewMember(crew_id=crew.id, user_id=owner.id, joined_at=inside),  # starting a crew isn't a join
        CrewMember(crew_id=crew.id, user_id=ben.id, joined_at=inside),
        CrewMember(crew_id=crew.id, user_id=cy.id, joined_at=outside),
        AppSetting(key=consolestats.PLAN_SHARES_MARKER, value={"at": "2026-11-01T00:00:00Z"}),
    ])
    db.flush()
    db.add_all([PlanShare(user_id=ben.id, event_id=ev.id, crew_id=crew.id, created_at=inside),
                PlanShare(user_id=cy.id, event_id=ev.id, crew_id=crew.id, created_at=None)])
    db.commit()
    r = get(client).json()
    assert (r["event_saves"], r["crew_invites"], r["crew_invite_accepts"], r["crew_suggestions"]) == (1, 3, 1, 1)
    assert r["new_members"] == 3  # the three users were made at Monday 00:00


def test_shares_are_null_for_weeks_before_they_were_dated(client, db):
    db.add(AppSetting(key=consolestats.PLAN_SHARES_MARKER, value={"at": "2026-11-12T12:00:00Z"}))
    db.commit()
    assert get(client).json()["crew_suggestions"] is None            # that week began before the marker
    assert get(client, week="2026-11-16").status_code == 400         # (and the next one is still running)


def test_the_response_is_counts_only(client, db):
    user(db, 1, MON_START)
    db.commit()
    body = get(client).json()
    assert set(body) == {"week_start", "week_end", "site_visits", "new_members", "event_saves", "crew_invites",
                         "crew_invite_accepts", "crew_suggestions"}
    assert all(isinstance(v, (int, type(None))) or k.startswith("week_") for k, v in body.items())
    assert "example.com" not in str(body)


def test_resharing_keeps_the_original_date(client, db, make_event):
    from app import crewplans
    owner = user(db, 1, MON_START)
    ev = make_event()
    crew = Crew(name="C", owner_id=owner.id)
    db.add(crew)
    db.flush()
    db.add(Plan(user_id=owner.id, event_id=ev.id, status=PlanStatus.interested))
    db.flush()
    old = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    db.add(PlanShare(user_id=owner.id, event_id=ev.id, crew_id=crew.id, created_at=old))
    db.commit()
    crewplans.share_plan(db, owner, ev.id, [crew.id])
    db.commit()
    db.expire_all()
    got = db.get(PlanShare, (owner.id, ev.id, crew.id)).created_at
    assert as_utc(got) == old
