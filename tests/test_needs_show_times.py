"""CC-311: GET /admin/events?needs_show_times=true lists live events with no upcoming show that has a start time."""
from datetime import timedelta

from sqlalchemy import text

from app.clock import today_in_chicago, utcnow
from app.models import EventRunDate, EventStatus

from .conftest import auth

T = today_in_chicago()
URL = "/admin/events?needs_show_times=true&limit=500"


def titles(client, token):
    r = client.get(URL, headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])
    return {e["title"] for e in body["items"]}


def add_show(db, ev, day, time):
    db.add(EventRunDate(event_id=ev.id, date=day, time=time))
    db.commit()


def test_events_with_only_dates_or_untimed_shows_are_listed(client, admin_token, make_event, db):
    make_event("Dates only")
    untimed = make_event("Untimed show")
    add_show(db, untimed, T + timedelta(days=2), "")
    weekly = make_event("Weekly only")
    weekly.weekly = {"Fri": ["19:30"]}
    db.commit()
    assert titles(client, admin_token) == {"Dates only", "Untimed show", "Weekly only"}


def test_each_exclusion(client, admin_token, make_event, db):
    make_event("Visual", category="Visual Art")
    make_event("Past", begin=T - timedelta(days=30), end=T - timedelta(days=2))
    make_event("Pending", status=EventStatus.pending)
    make_event("Rejected", status=EventStatus.rejected)
    make_event("Scheduled").publish_at = utcnow() + timedelta(days=3)  # approved, not yet published
    open_weekly = make_event("Open weekly", end=None)
    open_weekly.open_run, open_weekly.weekly = True, {"Sat": ["20:00"]}
    make_event("Open no pattern", end=None)  # an open run with no pattern of its own still needs times
    add_show(db, make_event("Timed"), T + timedelta(days=1), "19:30")
    db.commit()
    assert titles(client, admin_token) == {"Open no pattern"}


def test_an_event_leaves_the_list_once_an_upcoming_show_has_a_time(client, admin_token, make_event, db):
    ev = make_event("Hamlet")
    add_show(db, ev, T - timedelta(days=1), "19:30")  # a timed show in the past doesn't count
    assert titles(client, admin_token) == {"Hamlet"}
    add_show(db, ev, T, "19:30")  # today counts as upcoming
    assert titles(client, admin_token) == set()


def test_the_count_matches_a_direct_database_count(client, admin_token, make_event, db):
    make_event("A")
    make_event("B", category="Visual Art")
    add_show(db, make_event("C"), T + timedelta(days=1), "20:00")
    make_event("D", end=None).weekly = {"Mon": ["19:00"]}  # no closing date and its own pattern: allowed to be open
    add_show(db, make_event("E"), T + timedelta(days=1), "")
    make_event("F").weekly = {"Tue": ["19:00"]}  # a closing date and only a weekly pattern: needs times
    db.commit()
    direct = db.execute(text(  # written apart from the app's condition, like the CC-306 audit's recount
        "SELECT COUNT(*) FROM events e WHERE e.status = 'approved' AND (e.publish_at IS NULL OR e.publish_at <= :now)"
        " AND e.category <> 'Visual Art' AND (e.end_date IS NULL OR e.end_date >= :today)"
        " AND NOT ((e.open_run OR e.end_date IS NULL) AND CAST(e.weekly AS TEXT) NOT IN ('', '{}', 'null'))"
        " AND NOT EXISTS (SELECT 1 FROM event_run_dates r WHERE r.event_id = e.id AND r.date >= :today AND r.time <> '')"),
        {"today": T, "now": utcnow()}).scalar()
    body = client.get(URL, headers=auth(admin_token)).json()
    assert body["total"] == direct == 3
    assert {e["title"] for e in body["items"]} == {"A", "E", "F"}


def test_admin_only_and_the_public_list_is_unchanged(client, user_token, make_event):
    make_event("Hamlet")
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=auth(user_token)).status_code == 403
    assert [e["title"] for e in client.get("/events").json()["items"]] == ["Hamlet"]
