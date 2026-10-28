"""CC-88: found events need a date, and performances need show days and times, before they're approved;
a found event that's missing them has its own page read again."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.discovery import run as runner
from app.discovery.needs import needs
from app.discovery.readers import FoundEvent, Performance, ShowDay
from app.discovery.recap import build_recap, show_days_text
from app.discovery.rules import apply_rules
from app.discovery.store import record
from app.models import DiscoveredItem, DiscoveryCheck, Event

from .conftest import auth
from .test_discovery import TODAY, FakeClaude, make_source, pages  # noqa: F401 - pages is a fixture


def found(title, url="", **kw):
    return FoundEvent(title=title, url=url, kind=kw.pop("kind", "production"), category=kw.pop("category", "Theater"), **kw)


def item_named(db, title):
    return db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == title))


def no_show_times(source):
    source.venue.show_times = {}


def test_what_a_found_event_needs(db):
    s = make_source(db)
    no_show_times(s)
    cases = {
        "Undated play": found("Undated play"),
        "Dated play": found("Dated play", begin_date="2027-01-10"),
        "Play with show days": found("Play with show days", begin_date="2027-01-10", show_days=[ShowDay(weekday="thu", times=["19:30"])]),
        "Play with timed dates": found("Play with timed dates", performances=[Performance(date="2027-01-10", time="19:30")]),
        "Weekly open mic": found("Weekly open mic", kind="comedy_show", category="Comedy", open_run=True,
                                 show_days=[ShowDay(weekday="mon", times=["20:00"])]),
        "Undated exhibition": found("Undated exhibition", kind="exhibition", category="Visual Art"),
        "Open-run exhibition": found("Open-run exhibition", kind="exhibition", category="Visual Art", open_run=True),
        "Gallery": found("Gallery", kind="permanent", category="Visual Art"),
    }
    for e in cases.values():
        record(db, s, e, "")
    db.commit()
    got = {t: needs(item_named(db, t), s.venue) for t in cases}
    assert got == {"Undated play": "date", "Dated play": "show_days", "Play with show days": "", "Play with timed dates": "",
                   "Weekly open mic": "", "Undated exhibition": "date", "Open-run exhibition": "", "Gallery": ""}
    s.venue.show_times = {"thu": ["19:30"]}  # the venue's usual show times are enough
    assert needs(item_named(db, "Dated play"), s.venue) == ""
    assert item_named(db, "Play with show days").weekly == {"thu": ["19:30"]}


def test_a_repeating_performance_is_kept_but_generic_offerings_are_still_left_out():
    weekly = [ShowDay(weekday="tue", times=["20:00"])]
    events = [found("Tuesday Night Standup", kind="comedy_show", category="Comedy", recurring=True, show_days=weekly),
              found("Family Day", kind="program", category=None, recurring=True)]
    assert {e.title: why for e, why in apply_rules(events, "comedy")} == {"Tuesday Night Standup": "",
                                                                         "Family Day": "a recurring generic offering"}


def test_a_closer_look_doesnt_erase_what_the_list_page_gave(db):
    s = make_source(db)
    record(db, s, found("Hamlet", "https://goodman.example/hamlet", begin_date="2027-01-10", cost_text="$25"), "")
    record(db, s, found("Hamlet", "https://goodman.example/hamlet", show_days=[ShowDay(weekday="fri", times=["20:00", "8:00"])]), "")
    db.commit()
    item = item_named(db, "Hamlet")
    assert (str(item.begin_date), item.cost_text, item.weekly) == ("2027-01-10", "$25", {"fri": ["08:00", "20:00"]})


def test_show_days_found_for_a_listed_event_without_them_are_proposed(client, admin_token, db, make_event):
    s = make_source(db)
    listed = make_event("Othello", venue_name="Goodman Theatre", neighborhood="Loop")
    record(db, s, found("Othello", show_days=[ShowDay(weekday="sat", times=["14:00", "19:30"])]), "")
    db.commit()
    item = item_named(db, "Othello")
    assert item.status == "update" and item.changes == {"weekly": [None, {"sat": ["14:00", "19:30"]}]}
    assert show_days_text(item.changes["weekly"][1]) == "Sat 2 PM and 7:30 PM"
    r = client.post(f"/admin/discovery/items/{item.id}/accept", headers=auth(admin_token))
    db.expire_all()
    assert r.status_code == 200 and db.get(Event, listed.id).weekly == {"sat": ["14:00", "19:30"]}


def test_needs_a_date_list_and_approving_asks_for_whats_missing(client, admin_token, db):
    s = make_source(db)
    no_show_times(s)
    record(db, s, found("Undated play", "https://goodman.example/undated"), "")
    record(db, s, found("Ready play", "https://goodman.example/ready", begin_date="2027-01-10",
                        show_days=[ShowDay(weekday="thu", times=["19:30"])]), "")
    db.commit()
    h = auth(admin_token)
    r = client.get("/admin/discovery/items?show=needs", headers=h).json()
    assert [(i["title"], i["needs"]) for i in r["items"]] == [("Undated play", "date")] and r["total"] == 1
    assert r["counts"]["needs"] == 1 and r["counts"]["new"] == 2
    ready = next(i for i in client.get("/admin/discovery/items", headers=h).json()["items"] if i["title"] == "Ready play")
    assert ready["needs"] == "" and ready["weekly"] == {"thu": ["19:30"]} and ready["dates_timed"] is False

    item = item_named(db, "Undated play")
    body = {"title": "Undated play", "category": "Theater", "venue_id": s.venue_id, "url": item.url, "publish": "now"}
    r = client.post(f"/admin/discovery/items/{item.id}/approve", json=body, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "Add the date it opens."
    r = client.post(f"/admin/discovery/items/{item.id}/approve", json={**body, "begin_date": "2027-02-01"}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "Choose the days it's on, with a start time for each."
    r = client.post(f"/admin/discovery/items/{item.id}/approve",
                    json={**body, "begin_date": "2027-02-01", "weekly": {"fri": ["20:00"], "sat": ["20:00", "22:30"]}}, headers=h)
    assert r.status_code == 200
    assert db.get(Event, r.json()["event"]["id"]).weekly == {"fri": ["20:00"], "sat": ["20:00", "22:30"]}


def test_members_can_give_show_days_but_dont_have_to(client, user_token, db):
    body = {"title": "New Play", "category": "Theater", "begin_date": "2030-01-10",
            "venue": {"name": "Tiny Stage", "neighborhood": "Oak Park, IL"}}
    assert client.post("/events", json=body, headers=auth(user_token)).status_code == 201
    r = client.post("/events", json={**body, "title": "Weekly Play", "weekly": {"thu": ["19:30"]}}, headers=auth(user_token))
    assert r.status_code == 201 and db.get(Event, r.json()["id"]).weekly == {"thu": ["19:30"]}
    bad = client.post("/events", json={**body, "weekly": {"thu": ["7:30 PM"]}}, headers=auth(user_token))
    assert bad.status_code == 422  # times are sent as 24-hour HH:MM


def test_missing_items_have_their_page_read_the_next_day_then_weekly_for_four_weeks(db, pages):
    served, calls = pages
    s = make_source(db)
    no_show_times(s)
    url = "https://goodman.example/undated"
    record(db, s, found("Undated play", url), "")
    item = item_named(db, "Undated play")
    item.first_seen_at = datetime(2026, 10, 20, 15, tzinfo=UTC)  # 10 AM in Chicago, on TODAY
    s.next_check_on = TODAY + timedelta(days=365)  # the list page isn't due
    db.commit()
    served[url] = {"html": "<p>Undated play</p>"}
    claude = FakeClaude([found("Undated play", url)])

    def event_pages(day):
        calls.clear()
        runner.run(db, claude, day)
        return [u for u, _ in calls]

    assert event_pages(TODAY) == []  # found today: the list page was just read
    assert event_pages(TODAY + timedelta(days=1)) == [url]
    assert event_pages(TODAY + timedelta(days=4)) == []  # not a week yet
    assert event_pages(TODAY + timedelta(days=8)) == [url]
    check = db.scalars(select(DiscoveryCheck).order_by(DiscoveryCheck.id.desc())).first()
    assert check.what == "detail" and check.pages_read == 1 and "Undated play: missing a date" in check.note
    assert event_pages(TODAY + timedelta(days=15)) == [url] and event_pages(TODAY + timedelta(days=22)) == [url]
    assert event_pages(TODAY + timedelta(days=29)) == []  # four weeks are up; it stays marked, waiting for an admin
    assert item_named(db, "Undated play").status == "new"

    # once a reading finds the date (and the venue's show times cover the times), the mark goes away
    record(db, s, found("Undated play", url, begin_date="2027-03-01"), "")
    s.venue.show_times = {"fri": ["20:00"]}
    db.commit()
    assert needs(item_named(db, "Undated play"), s.venue) == ""


def test_the_recap_counts_events_waiting_for_a_date(db):
    s = make_source(db)
    no_show_times(s)
    record(db, s, found("Undated play"), "")
    record(db, s, found("Another undated play"), "")
    db.commit()
    email = build_recap(db)
    assert "2 events need a date or show days. We read their pages again each week for up to 4 weeks:" in email.text
    assert "found/needs" in email.text and "2 events need" in email.html


def test_show_days_on_some_weeks_of_the_month(client, admin_token, user_token, db, make_event):
    s = make_source(db)
    no_show_times(s)
    record(db, s, found("First and Third", "https://goodman.example/ft", kind="concert", category="Music", open_run=True,
                        show_days=[ShowDay(weekday="thu", times=["20:00"], weeks=[3, 1, 9]), ShowDay(weekday="sun", times=["19:00"])]), "")
    db.commit()
    item = item_named(db, "First and Third")
    assert item.weekly == {"thu": ["20:00"], "sun": ["19:00"]} and item.weeks == {"thu": [1, 3]}  # Sunday: every week
    assert show_days_text(item.weekly, {"thu": [1, 3], "sun": [5]}) == "1st and 3rd Thu 8 PM; last Sun 7 PM"

    h = auth(admin_token)
    body = {"title": "First and Third", "category": "Music", "venue_id": s.venue_id, "open_run": True, "publish": "now",
            "weekly": {"thu": ["20:00"]}, "weeks": {"thu": [3, 1], "fri": [2]}}  # Friday isn't a show day, so it's dropped
    r = client.post(f"/admin/discovery/items/{item.id}/approve", json=body, headers=h)
    event = db.get(Event, r.json()["event"]["id"])
    assert r.status_code == 200 and event.weeks == {"thu": [1, 3]}
    assert client.get(f"/events/{event.id}").json()["schedule"]["weeks"] == {"thu": [1, 3]}

    # editing: weeks for a day that's no longer a show day go; {} is every week again
    assert client.patch(f"/events/{event.id}", json={"weekly": {"sun": ["19:00"]}}, headers=h).status_code == 200
    db.expire_all()
    assert db.get(Event, event.id).weeks == {}
    assert client.patch(f"/events/{event.id}", json={"weekly": {"sun": ["19:00"]}, "weeks": {"sun": [5]}}, headers=h).json()["schedule"]["weeks"] == {"sun": [5]}
    assert client.patch(f"/events/{event.id}", json={"weeks": {"sun": [6]}}, headers=h).status_code == 422

    # members can send them too
    sub = {"title": "Monthly Play", "category": "Theater", "begin_date": "2030-01-10", "venue": {"name": "Tiny Stage"},
           "weekly": {"sat": ["14:00"]}, "weeks": {"sat": [2]}}
    r = client.post("/events", json=sub, headers=auth(user_token))
    assert r.status_code == 201 and db.get(Event, r.json()["id"]).weeks == {"sat": [2]}

    # found for an event already listed without show days: both are proposed, and Accept applies both
    listed = make_event("Monthly Jam", venue_name="Goodman Theatre", neighborhood="Loop")
    record(db, s, found("Monthly Jam", show_days=[ShowDay(weekday="tue", times=["21:00"], weeks=[5])]), "")
    db.commit()
    jam = item_named(db, "Monthly Jam")
    assert jam.changes == {"weekly": [None, {"tue": ["21:00"]}], "weeks": [None, {"tue": [5]}]}
    client.post(f"/admin/discovery/items/{jam.id}/accept", headers=h)
    db.expire_all()
    assert (db.get(Event, listed.id).weekly, db.get(Event, listed.id).weeks) == ({"tue": ["21:00"]}, {"tue": [5]})
