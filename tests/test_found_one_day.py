"""CC-212: approving a found one-day event with a start time, whatever its category. The site sends the time as a
one-date run (CC-96's run_dates), so the event page and the planner show it the way they show performance times."""
from datetime import timedelta

from app.discovery.readers import Performance
from app.discovery.store import record
from app.models import Event
from app.stats import today_in_chicago

from .conftest import auth
from .test_discovery import make_source
from .test_found_needs import found, item_named, no_show_times


def approve(client, token, item, venue_id, day, **kw):
    body = {"title": item.title, "category": "Visual Art", "venue_id": venue_id, "begin_date": day, "end_date": day,
            "url": item.url, "publish": "now", **kw}
    return client.post(f"/admin/discovery/items/{item.id}/approve", json=body, headers=auth(token))


def test_a_one_day_exhibition_approved_with_a_start_time(client, admin_token, db):
    s = make_source(db)
    day = (today_in_chicago() + timedelta(days=10)).isoformat()
    record(db, s, found("Opening Night", "https://gallery.example/opening", kind="exhibition", category="Visual Art",
                        begin_date=day, end_date=day), ""); db.commit()
    item = item_named(db, "Opening Night")
    r = approve(client, admin_token, item, s.venue_id, day, run_dates=[{"date": day, "time": "18:00"}])
    assert r.status_code == 200
    event = client.get(f"/events/{r.json()['event']['id']}").json()
    assert event["begin_date"] == event["end_date"] == day
    assert event["run_dates"] == [{"date": day, "time": "18:00", "end_time": ""}]
    assert event["schedule"]["dates"] == [{"date": day, "time": "18:00"}]


def test_a_one_day_exhibition_approved_without_a_start_time(client, admin_token, db):
    s = make_source(db)
    day = (today_in_chicago() + timedelta(days=10)).isoformat()
    record(db, s, found("Artist Talk", "https://gallery.example/talk", kind="exhibition", category="Visual Art",
                        begin_date=day, end_date=day), ""); db.commit()
    item = item_named(db, "Artist Talk")
    r = approve(client, admin_token, item, s.venue_id, day)
    assert r.status_code == 200
    event = db.get(Event, r.json()["event"]["id"])
    assert event.run_dates == [] and event.begin_date.isoformat() == event.end_date.isoformat() == day


def test_a_one_day_show_can_take_its_start_time_instead_of_show_days(client, admin_token, db):
    s = make_source(db)
    no_show_times(s)
    day = (today_in_chicago() + timedelta(days=10)).isoformat()
    record(db, s, found("One Night Only", "https://goodman.example/one", begin_date=day, end_date=day), ""); db.commit()
    item = item_named(db, "One Night Only")
    r = approve(client, admin_token, item, s.venue_id, day, category="Theater")
    assert r.status_code == 422 and r.json()["detail"] == "Choose the days it's on, with a start time for each."  # as before
    r = approve(client, admin_token, item, s.venue_id, day, category="Theater", run_dates=[{"date": day, "time": "19:30"}])
    assert r.status_code == 200
    assert [(d.date.isoformat(), d.time) for d in db.get(Event, r.json()["event"]["id"]).run_dates] == [(day, "19:30")]


def test_a_show_on_for_under_a_week_is_approved_on_its_days_with_their_times(client, admin_token, db):
    s = make_source(db)
    no_show_times(s)
    first = today_in_chicago() + timedelta(days=10)
    days = [(first + timedelta(days=n)).isoformat() for n in (0, 1, 3)]  # Thursday, Friday and Sunday, say: not every day
    record(db, s, found("Short Run", "https://goodman.example/short", begin_date=days[0], end_date=days[-1]), ""); db.commit()
    item = item_named(db, "Short Run")
    run = [{"date": d, "time": t} for d, t in zip(days, ("19:30", "19:30", "14:00"))]
    r = approve(client, admin_token, item, s.venue_id, days[0], category="Theater", run_dates=run)
    assert r.status_code == 200
    event = client.get(f"/events/{r.json()['event']['id']}").json()
    assert (event["begin_date"], event["end_date"]) == (days[0], days[-1])
    assert [(d["date"], d["time"]) for d in event["run_dates"]] == [(d["date"], d["time"]) for d in run]


def test_found_items_on_for_under_a_week_list_their_dates_for_the_form(client, admin_token, db):
    s = make_source(db)
    first = today_in_chicago() + timedelta(days=10)
    iso = lambda n: (first + timedelta(days=n)).isoformat()
    shows = [Performance(date=iso(0), time="19:30"), Performance(date=iso(2), time="14:00")]
    record(db, s, found("Weekend", "https://goodman.example/weekend", begin_date=iso(0), end_date=iso(2), performances=shows), "")
    record(db, s, found("Long Run", "https://goodman.example/long", begin_date=iso(0), end_date=iso(6), performances=shows), "")
    db.commit()
    by = {i["title"]: i for i in client.get("/admin/discovery/items", headers=auth(admin_token)).json()["items"]}
    assert by["Weekend"]["short_dates"] == [{"date": iso(0), "time": "19:30"}, {"date": iso(2), "time": "14:00"}]
    assert by["Long Run"]["short_dates"] == []  # a week or more: the form starts as a date range, as before
