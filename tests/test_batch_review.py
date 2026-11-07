"""CC-208: batch review of found events on the Event Sourcing page: approve what's ready, delete, hide, and the By day order."""
from sqlalchemy import select

from app.discovery.readers import FoundEvent, Performance, ShowDay
from app.discovery.store import record
from app.models import DiscoveredItem, Event, EventStatus, Venue
from app.stats import today_in_chicago

from .conftest import auth
from .test_discovery import make_source


SATURDAYS = [ShowDay(weekday="sat", times=["19:30"])]


def found(title, **kw):
    return FoundEvent(title=title, url=kw.pop("url", f"https://goodman.example/{title.lower().replace(' ', '-')}"), kind=kw.pop("kind", "production"),
                      category=kw.pop("category", "Theater"), **{"begin_date": "2027-01-10", "end_date": "2027-02-14", **kw})


def add(db, source, *events):
    for e in events:
        record(db, source, e, "")
    db.commit()
    return [db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == e.title)) for e in events]


def batch(client, token, action, items):
    return client.post("/admin/discovery/items/batch", json={"action": action, "ids": [i.id for i in items]}, headers=auth(token))


def listed(client, token, query="show=new"):
    return client.get(f"/admin/discovery/items?{query}", headers=auth(token)).json()


def held(res):
    return {h["id"]: h["why"] for h in res.json()["held"]}


def test_batch_approve_adds_ready_events_live_with_what_was_found(client, db, admin_token):
    s = make_source(db)
    hamlet, macbeth = add(db, s, found("Hamlet", cost_text="$25", min_price=25, show_days=SATURDAYS), found("Macbeth"))
    r = batch(client, admin_token, "approve", [hamlet, macbeth])
    assert r.status_code == 200 and r.json() == {"done": [hamlet.id, macbeth.id], "held": []}
    db.expire_all()
    assert hamlet.status == "approved" and macbeth.status == "approved"
    event = db.get(Event, hamlet.event_id)
    assert event.status == EventStatus.approved and event.found_by_id == s.id and event.venue_id == s.venue_id
    assert (event.title, event.cost_text, event.min_price, event.url) == ("Hamlet", "$25", 25, hamlet.url)
    assert event.weekly == {"sat": ["19:30"]}
    assert db.get(Event, macbeth.event_id).weekly == {}  # the venue's usual times stay the venue's


def test_batch_approve_holds_back_what_needs_a_person(client, db, admin_token, make_event):
    s = make_source(db)
    venue = db.get(Venue, s.venue_id)
    make_event(title="The Tempest Tour Again Today")
    venue.show_times = {}  # so a show with no times of its own is missing them
    ready, dup, other_venue, no_days, no_date = add(
        db, s, found("Hamlet", show_days=SATURDAYS), found("The Tempest Tour Again", show_days=SATURDAYS),
        found("Far Away", venue_name="Wheaton Hall", show_days=SATURDAYS), found("Macbeth"),
        found("Talk", kind="talk"))
    no_date.begin_date = no_date.end_date = None  # what the checker would leave out or fill in, set here by hand
    db.commit()
    r = batch(client, admin_token, "approve", [ready, dup, other_venue, no_days, no_date])
    assert r.json()["done"] == [ready.id]
    assert held(r) == {dup.id: "duplicate", other_venue.id: "new_venue", no_days.id: "show_days", no_date.id: "date"}
    db.expire_all()
    assert {i.status for i in (dup, other_venue, no_days, no_date)} == {"new"}
    assert db.scalar(select(Event).where(Event.title == "Macbeth")) is None
    # the list says the same before anything is sent, so the page can count what's ready
    by_id = {i["id"]: i["batch_hold"] for i in listed(client, admin_token)["items"]}
    assert by_id == {dup.id: "duplicate", other_venue.id: "new_venue", no_days.id: "show_days", no_date.id: "date"}


def test_one_with_no_type_waits_until_its_venue_has_one(client, db, admin_token):
    s = make_source(db)
    mystery, = add(db, s, found("Mystery"))
    mystery.category = None
    db.commit()
    assert held(batch(client, admin_token, "approve", [mystery])) == {mystery.id: "type"}
    db.get(Venue, s.venue_id).category = "Theater"  # the type a found event starts with in the form (CC-125)
    db.commit()
    assert batch(client, admin_token, "approve", [mystery]).json()["done"] == [mystery.id]


def test_a_short_run_goes_in_with_its_listed_times_or_waits_for_the_form(client, db, admin_token):
    s = make_source(db)
    timed = found("Gallery Talk", category="Visual Art", kind="talk", begin_date="2027-03-04", end_date="2027-03-05",
                  performances=[Performance(date="2027-03-04", time="18:00"), Performance(date="2027-03-05", time="12:30"), Performance(date="2027-03-05", time="15:00")])
    gap = found("Open Studio", category="Visual Art", kind="talk", begin_date="2027-03-04", end_date="2027-03-05",
                performances=[Performance(date="2027-03-04", time="18:00"), Performance(date="2027-03-05")])
    bare = found("Pop-up", category="Visual Art", kind="talk", begin_date="2027-03-04", end_date="2027-03-04")
    talk, studio, popup = add(db, s, timed, gap, bare)
    r = batch(client, admin_token, "approve", [talk, studio, popup])
    assert r.json()["done"] == [talk.id] and held(r) == {studio.id: "short_run", popup.id: "short_run"}
    db.expire_all()
    event = db.get(Event, talk.event_id)
    assert [(d.date.isoformat(), d.time) for d in event.run_dates] == [("2027-03-04", "18:00"), ("2027-03-05", "12:30")]
    assert len(event.performances) == 3  # every listed time still reaches the planner


def test_an_opera_source_and_the_venues_default_type_start_as_in_the_form(client, db, admin_token):
    s = make_source(db)
    db.get(Venue, s.venue_id).category = "Music"
    db.commit()
    concert, = add(db, s, found("Recital", category="Theater", show_days=SATURDAYS))
    concert.opera = True
    db.commit()
    assert batch(client, admin_token, "approve", [concert]).json()["held"] == []
    db.expire_all()
    event = db.get(Event, concert.event_id)
    assert event.category == "Music" and event.opera


def test_a_venue_and_an_event_that_disagree_on_type_wait_for_the_form(client, db, admin_token):
    s = make_source(db)
    db.get(Venue, s.venue_id).category = "Music"  # an exhibition must not go live as Music with nobody looking
    db.commit()
    show, = add(db, s, found("Monet", category="Visual Art", kind="exhibition"))
    assert held(batch(client, admin_token, "approve", [show])) == {show.id: "type_differs"}


def test_what_the_source_gave_that_doesnt_fit_waits_for_the_form(client, db, admin_token):
    s = make_source(db)
    odd, = add(db, s, found("Hamlet"))
    odd.url = "ftp://goodman.example/hamlet"
    db.commit()
    assert held(batch(client, admin_token, "approve", [odd])) == {odd.id: "form"}


def test_batch_delete_and_hide_and_the_counts_follow(client, db, admin_token):
    s = make_source(db)
    a, b, c = add(db, s, found("Hamlet"), found("Macbeth"), found("Othello"))
    assert batch(client, admin_token, "reject", [a]).json() == {"done": [a.id], "held": []}
    assert batch(client, admin_token, "hide", [b]).json() == {"done": [b.id], "held": []}
    db.expire_all()
    assert a.status == "rejected" and b.status == "hidden"
    assert listed(client, admin_token, "limit=1")["total"] == 1  # the Review count
    summary = client.get("/admin/discovery/summary", headers=auth(admin_token)).json()
    assert summary["new_waiting"] == 1 and summary["hidden"] == 1 and [r["n"] for r in summary["waiting_by_source"]] == [1]
    add(db, s, found("Hamlet"))  # a later check doesn't bring a deleted one back
    db.expire_all()
    assert a.status == "rejected"


def test_one_someone_else_decided_is_left_alone_and_the_rest_go_through(client, db, admin_token):
    s = make_source(db)
    a, b, c = add(db, s, found("Hamlet"), found("Macbeth"), found("Othello"))
    b.status = "rejected"
    db.commit()
    r = client.post("/admin/discovery/items/batch", json={"action": "hide", "ids": [a.id, b.id, a.id, 99999, c.id]}, headers=auth(admin_token))
    assert r.json()["done"] == [a.id, c.id] and held(r) == {b.id: "gone", 99999: "gone"}
    db.expire_all()
    assert (a.status, b.status, c.status) == ("hidden", "rejected", "hidden")


def test_batches_are_limited_and_for_admins_only(client, db, admin_token, user_token):
    s = make_source(db)
    a, = add(db, s, found("Hamlet"))
    assert batch(client, user_token, "reject", [a]).status_code == 403
    assert client.post("/admin/discovery/items/batch", json={"action": "reject", "ids": [a.id]}).status_code == 401
    assert client.post("/admin/discovery/items/batch", json={"action": "reject", "ids": []}, headers=auth(admin_token)).status_code == 422
    assert client.post("/admin/discovery/items/batch", json={"action": "accept", "ids": [a.id]}, headers=auth(admin_token)).status_code == 422
    assert client.post("/admin/discovery/items/batch", json={"action": "hide", "ids": list(range(501))}, headers=auth(admin_token)).status_code == 422
    db.expire_all()
    assert a.status == "new"


def test_by_day_goes_by_date_with_undated_events_last_and_pages_through(client, db, admin_token):
    s = make_source(db)
    today = today_in_chicago()
    late, early, closing, undated = add(db, s, found("Late", begin_date="2027-05-01", end_date=None), found("Early", begin_date=today.isoformat(), end_date=None),
                                        found("Closing", begin_date=None, end_date="2027-02-01"), found("Undated", begin_date=None, end_date=None))
    q = "show=new&order=day"
    assert [i["title"] for i in listed(client, admin_token, q)["items"]] == ["Early", "Closing", "Late", "Undated"]
    page = listed(client, admin_token, q + "&limit=2&offset=2")
    assert [i["title"] for i in page["items"]] == ["Late", "Undated"] and page["total"] == 4
    assert listed(client, admin_token)["items"][0]["title"] == "Undated"  # newest found first stays the default
