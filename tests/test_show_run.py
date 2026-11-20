"""CC-301: the Show Run: every performance of an event in one store (event_run_dates), saved through PATCH
/events/{id} `show_run`, read back as `show_run`, and used by Find an outing, the roundups and the calendar file."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.discovery.readers import FoundEvent, Performance
from app.discovery.store import changes_against, record, sync_found_shows
from app.models import DiscoveredItem, Event, EventRunDate
from app.roundups import times_on
from app.stats import today_in_chicago

from .conftest import auth
from .test_discovery import make_source

T = today_in_chicago()


def day(n):
    return T + timedelta(days=n)


def show(n, time="20:00", **kw):
    return {"date": day(n).isoformat(), "time": time, **kw}


def patch(client, token, event_id, **body):
    return client.patch(f"/events/{event_id}", json=body, headers=auth(token))


def shows_of(client, event_id):
    return client.get(f"/events/{event_id}").json()["show_run"]


def seed_shows(db, event, *rows, origin="admin"):
    event.shows = [EventRunDate(date=d, time=t, origin=origin) for d, t in rows]
    db.commit()


# ---- saving ------------------------------------------------------------------------------------------------------

def test_forty_five_shows_save_and_reload_with_begin_end_from_the_first_and_last(client, admin_token, make_event):
    ev = make_event("Long Run", begin=day(-2), end=day(100))
    ev_id = ev.id
    run = [show(n) for n in range(1, 46)]
    r = patch(client, admin_token, ev_id, show_run=run, open_run=True, weekly={"thu": ["19:30"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["show_run"] == [{**s, "end_time": ""} for s in run]  # POST/PATCH carry it too
    assert (body["begin_date"], body["end_date"], body["open_run"], body["schedule"]["weekly_from"]) == (day(1).isoformat(), day(45).isoformat(), False, "")
    got = client.get(f"/events/{ev_id}").json()
    assert got["show_run"] == body["show_run"] and len(got["schedule"]["dates"]) == 45
    assert (got["begin_date"], got["end_date"], got["open_run"]) == (day(1).isoformat(), day(45).isoformat(), False)
    assert got["schedule"]["dates_from"] == "event"


def test_retime_and_remove_then_reload(client, admin_token, make_event):
    ev = make_event("Odd Nights", begin=None, end=None)
    patch(client, admin_token, ev.id, show_run=[show(1), show(2), show(3)])
    r = patch(client, admin_token, ev.id, show_run=[show(1, "19:00", end_time="21:00"), show(3)])  # retimed, one removed
    assert r.status_code == 200, r.text
    assert shows_of(client, ev.id) == [{"date": day(1).isoformat(), "time": "19:00", "end_time": "21:00"},
                                       {"date": day(3).isoformat(), "time": "20:00", "end_time": ""}]
    assert client.get(f"/events/{ev.id}").json()["end_date"] == day(3).isoformat()


def test_two_shows_a_day_and_an_untimed_show_are_accepted_and_sorted(client, admin_token, make_event):
    ev = make_event("Matinee", begin=None, end=None)
    r = patch(client, admin_token, ev.id, show_run=[show(2, "20:00"), show(2, "14:00"), show(1, "")])
    assert r.status_code == 200, r.text
    assert [(s["date"], s["time"]) for s in r.json()["show_run"]] == [(day(1).isoformat(), ""), (day(2).isoformat(), "14:00"), (day(2).isoformat(), "20:00")]
    sched = client.get(f"/events/{ev.id}").json()["schedule"]["dates"]
    assert [(d["date"], d["time"]) for d in sched] == [(day(1).isoformat(), None), (day(2).isoformat(), "14:00"), (day(2).isoformat(), "20:00")]


@pytest.mark.parametrize("run", [
    [show(1, "20:00", end_time="19:00")],  # ends before it starts
    [show(1, "", end_time="21:00")],  # an end with no start
    [show(1), show(1)],  # a duplicate
    [show(1, f"{h:02d}:00") for h in range(10, 17)],  # seven in a day
    [show(1, ""), show(1, "20:00")],  # an untimed show shares its day
    [show(n) for n in range(1, 502)],  # one over the cap
    [show(-3)],  # a new past day
    [show(1, "8pm")],
], ids=["end-before-start", "end-without-start", "duplicate", "seven-a-day", "untimed-with-timed", "501", "past", "bad-time"])
def test_these_are_rejected(client, admin_token, make_event, run):
    ev = make_event("Odd Nights", begin=None, end=None)
    assert patch(client, admin_token, ev.id, show_run=run).status_code == 422
    assert shows_of(client, ev.id) == []


def test_six_a_day_and_five_hundred_are_fine(client, admin_token, make_event):
    ev = make_event("Festival", begin=None, end=None)
    assert patch(client, admin_token, ev.id, show_run=[show(1, f"{h:02d}:00") for h in range(10, 16)]).status_code == 200
    assert patch(client, admin_token, ev.id, show_run=[show(n) for n in range(1, 501)]).status_code == 200
    assert len(shows_of(client, ev.id)) == 500


def test_a_kept_past_day_is_fine_a_new_one_is_not(client, admin_token, make_event, db):
    ev = make_event("Odd Nights", begin=None, end=None)
    seed_shows(db, ev, (day(-3), "20:00"), (day(2), "20:00"))
    assert patch(client, admin_token, ev.id, show_run=[show(-3, "19:00"), show(2)]).status_code == 200  # a past day it had
    assert patch(client, admin_token, ev.id, show_run=[show(-3, "19:00"), show(-2), show(2)]).status_code == 422


def test_show_run_with_run_dates_null_or_a_non_admin_is_refused(client, admin_token, user_token, make_event):
    ev = make_event("Odd Nights", begin=None, end=None)
    assert patch(client, admin_token, ev.id, show_run=[show(1)], run_dates=[show(1)]).status_code == 422
    assert patch(client, admin_token, ev.id, show_run=None).status_code == 422
    assert patch(client, user_token, ev.id, show_run=[show(1)]).status_code == 403


def test_emptying_the_run_with_null_dates_lists_dates_not_posted(client, admin_token, make_event):
    ev = make_event("Odd Nights", begin=None, end=None)
    patch(client, admin_token, ev.id, show_run=[show(1), show(2)])
    r = patch(client, admin_token, ev.id, show_run=[], begin_date=None, end_date=None)
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["show_run"], b["begin_date"], b["end_date"], b["open_run"]) == ([], None, None, False)
    assert b["schedule"]["dates"] == [] and b["schedule"]["dates_from"] == ""
    patch(client, admin_token, ev.id, show_run=[show(1), show(2)])
    assert patch(client, admin_token, ev.id, show_run=[]).json()["begin_date"] == day(1).isoformat()  # left as they were


def test_a_show_run_save_replaces_found_shows(client, admin_token, make_event, db):
    ev = make_event("Hamlet", begin=day(-3), end=day(40))
    seed_shows(db, ev, (day(5), "19:30"), (day(6), "19:30"), origin="found")
    assert client.get(f"/events/{ev.id}").json()["schedule"]["dates_from"] == "found"
    patch(client, admin_token, ev.id, show_run=[show(9)])
    db.expire_all()
    assert [(r.date, r.origin) for r in db.get(Event, ev.id).shows] == [(day(9), "admin")]


# ---- reading -----------------------------------------------------------------------------------------------------

def test_lists_do_not_carry_the_show_run_and_legacy_run_dates_leave_out_found_and_untimed(client, admin_token, make_event, db):
    exact = make_event("Exact", begin=day(1), end=day(3))
    seed_shows(db, exact, (day(1), "20:00"), (day(2), ""), (day(3), "19:00"))
    found = make_event("Found", begin=day(1), end=day(30))
    seed_shows(db, found, (day(5), "19:30"), origin="found")
    items = {e["title"]: e for e in client.get("/events", params={"limit": 100}).json()["items"]}
    assert items["Exact"]["show_run"] == [] and items["Found"]["show_run"] == []  # lists: left out to stay small
    assert [d["time"] for d in items["Exact"]["run_dates"]] == ["20:00", "19:00"]  # legacy: timed shows of an exact run
    assert items["Found"]["run_dates"] == []
    assert len(client.get(f"/events/{exact.id}").json()["show_run"]) == 3
    assert len(client.get(f"/events/{found.id}").json()["show_run"]) == 1


def test_dates_from_is_event_found_or_empty(client, make_event, db):
    exact, found, none = make_event("Exact"), make_event("Found"), make_event("None")
    seed_shows(db, exact, (day(1), "20:00"))
    seed_shows(db, found, (day(1), "20:00"), origin="found")
    from_ = lambda e: client.get(f"/events/{e.id}").json()["schedule"]["dates_from"]  # noqa: E731
    assert (from_(exact), from_(found), from_(none)) == ("event", "found", "")


def test_upcoming_dates_go_up_to_five_hundred_and_skip_past_ones(client, make_event, db):
    ev = make_event("Marathon", begin=None, end=None)
    seed_shows(db, ev, *[(day(n), "20:00") for n in range(-5, 600)])
    dates = client.get(f"/events/{ev.id}").json()["schedule"]["dates"]
    assert len(dates) == 500 and dates[0]["date"] == day(0).isoformat()
    assert len(client.get(f"/events/{ev.id}").json()["show_run"]) == 605  # every show, past ones too


def test_find_an_outing_dates_match_only_show_days_for_an_exact_run_but_not_for_found_only(client, make_event, db):
    exact = make_event("Exact", begin=day(1), end=day(10))
    seed_shows(db, exact, (day(1), "20:00"), (day(10), "20:00"))
    found = make_event("Found", begin=day(-1), end=day(30))
    seed_shows(db, found, (day(1), "20:00"), (day(10), "20:00"), origin="found")
    titles = lambda a, b: sorted(e["title"] for e in client.get("/events", params={"from": day(a), "to": day(b)}).json()["items"])  # noqa: E731
    assert titles(5, 5) == ["Found"]  # between its shows: an exact run is off, a found-only one is left alone
    assert titles(1, 1) == ["Exact", "Found"]
    assert titles(10, 12) == ["Exact", "Found"]


# ---- legacy input writes the same store ----------------------------------------------------------------------------

def test_legacy_run_dates_write_shows_with_the_origin_of_the_sender(client, admin_token, user_token, make_event, db):
    venue = {"name": "Tiny Stage", "neighborhood": "Oak Park, IL"}
    body = {"title": "Odd Nights", "category": "Theater", "venue": venue, "run_dates": [show(2, "14:00"), show(2), show(5)]}
    mine = client.post("/events", json=body, headers=auth(user_token))
    assert mine.status_code == 201, mine.text
    assert [(s["date"], s["time"]) for s in mine.json()["show_run"]] == [(day(2).isoformat(), "14:00"), (day(2).isoformat(), "20:00"), (day(5).isoformat(), "20:00")]
    assert {r.origin for r in db.get(Event, mine.json()["id"]).shows} == {"member"}
    theirs = client.post("/events", json={**body, "title": "Odd Nights 2"}, headers=auth(admin_token))
    assert {r.origin for r in db.get(Event, theirs.json()["id"]).shows} == {"admin"}
    r = patch(client, admin_token, mine.json()["id"], run_dates=[show(7)])
    assert [r2["date"] for r2 in r.json()["show_run"]] == [day(7).isoformat()] and r.json()["begin_date"] == day(7).isoformat()
    assert {x.origin for x in db.get(Event, mine.json()["id"]).shows} == {"admin"}


def test_an_exact_run_keeps_begin_and_end_on_its_first_and_last_show_even_on_other_edits(client, admin_token, make_event, db):
    ev = make_event("Odd Nights", begin=day(-9), end=day(99))
    seed_shows(db, ev, (day(2), "20:00"), (day(8), "20:00"))
    r = patch(client, admin_token, ev.id, title="Odd Nights!", end_date=day(50).isoformat(), open_run=True)
    assert (r.json()["begin_date"], r.json()["end_date"], r.json()["open_run"]) == (day(2).isoformat(), day(8).isoformat(), False)


# ---- roundups and the calendar file --------------------------------------------------------------------------------

def test_times_on_for_exact_and_found_runs(make_event, db):
    exact = make_event("Exact", begin=day(1), end=day(20))
    exact.weekly = {k: ["19:00"] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    seed_shows(db, exact, (day(2), "20:00"), (day(2), "14:00"), (day(4), ""))
    assert times_on(exact, day(2)) == ["14:00", "20:00"]
    assert times_on(exact, day(4)) == []  # on, time not posted
    assert times_on(exact, day(3)) is None and times_on(exact, day(10)) is None  # never the weekly pattern
    found = make_event("Found", begin=day(1), end=day(20))
    found.weekly = {k: ["19:00"] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    seed_shows(db, found, (day(2), "20:00"), origin="found")
    assert times_on(found, day(2)) == ["20:00"]
    assert times_on(found, day(3)) == ["19:00"]  # a day with no found show falls back to the weekly pattern


def test_the_calendar_file_uses_the_shows(client, make_event, db):
    ev = make_event("Exact", begin=day(1), end=day(20))
    seed_shows(db, ev, (day(3), "18:30"))
    r = client.get(f"/events/{ev.id}/calendar.ics", params={"date": day(3).isoformat()})
    assert r.status_code == 200 and "T" in r.text and "couldn't find a time" not in r.text


def test_an_exact_event_closes_soon_by_its_last_show(client, admin_token, make_event):
    ev = make_event("Closing", begin=None, end=None)
    patch(client, admin_token, ev.id, show_run=[show(1), show(3)])
    page = client.get("/chicago/closing-soon")
    assert page.status_code == 200 and "Closing" in page.text and "Closes " in page.text


# ---- discovery -----------------------------------------------------------------------------------------------------

def found_event(*pairs, **kw):
    return FoundEvent(title="Hamlet", kind="production", category="Theater",
                      performances=[Performance(date=d.isoformat(), time=t) for d, t in pairs], **kw)


def test_found_dates_wait_for_review_on_a_live_event_and_never_touch_a_saved_run(db, make_event):
    s = make_source(db)
    ev = make_event("Hamlet", venue_name="Goodman Theatre", neighborhood="Loop", begin=day(1), end=day(90))
    record(db, s, found_event((day(5), "19:30"), (day(5), None), (day(6), None)), "")
    db.commit(); db.expire_all()
    ev = db.get(Event, ev.id)
    assert ev.shows == []  # CC-302: a live event's dates change only when an admin accepts (sync_found_shows is for approval)
    item = db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == "Hamlet"))
    assert item.status == "update" and item.changes["performances"][1] == {day(5).isoformat(): ["19:30"], day(6).isoformat(): [""]}
    assert (ev.begin_date, ev.end_date) == (day(1), day(90))
    ev.shows = [EventRunDate(date=day(9), time="18:00", origin="admin")]
    db.commit()
    record(db, s, found_event((day(11), "20:00")), "")
    db.commit(); db.expire_all()
    assert [(r.date, r.origin) for r in db.get(Event, ev.id).shows] == [(day(9), "admin")]  # a person's Show Run stays


def test_changes_against_and_accept_leave_an_exact_runs_dates_alone(client, admin_token, db, make_event):
    s = make_source(db)
    ev = make_event("Hamlet", venue_name="Goodman Theatre", neighborhood="Loop", begin=day(2), end=day(8))
    seed_shows(db, ev, (day(2), "20:00"), (day(8), "20:00"))
    f = {"cost_text": "$30", "min_price": None, "begin_date": day(1), "end_date": day(20), "weekly": {"fri": ["20:00"]}, "weeks": {}}
    assert set(changes_against(db.get(Event, ev.id), f)) == {"cost_text"}
    ev.shows = []
    db.commit()
    assert set(changes_against(db.get(Event, ev.id), f)) == {"cost_text", "begin_date", "end_date", "weekly"}  # not exact: as before
    # a stale proposal made before the run was saved: accepting it must not break the run
    record(db, s, FoundEvent(title="Hamlet", kind="production", category="Theater"), "")
    item = db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == "Hamlet"))
    item.event_id, item.status = ev.id, "update"
    item.changes = {"end_date": [day(8).isoformat(), day(30).isoformat()], "cost_text": ["", "$30"]}
    seed_shows(db, db.get(Event, ev.id), (day(2), "20:00"), (day(8), "20:00"))
    r = client.post(f"/admin/discovery/items/{item.id}/accept", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    db.expire_all()
    e = db.get(Event, ev.id)
    assert (e.end_date, e.cost_text) == (day(8), "$30")


def test_sync_found_shows_without_dates_does_nothing(db, make_event):
    ev = make_event("Hamlet")
    item = DiscoveredItem(event_id=ev.id, performances=[])
    sync_found_shows(db, item)  # no item.performances: returns before reading anything else
    assert db.get(Event, ev.id).shows == []


# ---- the QA fixture --------------------------------------------------------------------------------------------------

def test_the_qa_fixture_has_37_shows_is_repeatable_and_refuses_production(db, monkeypatch):
    from scripts import seed_show_run_qa as qa
    ev = qa.seed(db)
    assert len(ev.shows) == 37 and (ev.begin_date, ev.end_date) == (date(2026, 11, 5), date(2026, 12, 26))
    assert {r.origin for r in ev.shows} == {"admin"} and ev.show_run_exact and ev.category == "Theater"
    again = qa.seed(db)
    assert again.id == ev.id and len(again.shows) == 37
    assert db.scalar(select(Event.id).where(Event.title == "Show Run QA").limit(1)) == ev.id
    assert len(db.scalars(select(EventRunDate)).all()) == 37
    monkeypatch.setattr(qa, "get_settings", lambda: type("S", (), {"env": "prod"})())
    with pytest.raises(SystemExit):
        qa.main()
