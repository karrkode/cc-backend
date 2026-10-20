"""CC-78: venues' opening hours and usual show times: filled in by discovery, edited by admins."""
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from app.discovery import run as runner
from app.discovery.hours import show_times_from
from app.discovery.readers import FoundEvent, OpeningHours, PageReading, Performance
from app.models import DiscoveredItem, Venue

from .conftest import auth
from .test_discovery import TODAY, make_source, pages  # noqa: F401 - pages is a fixture

HOURS = {"mon": ["10:00", "17:00"], "tue": None, "thu": ["10:00", "20:00"]}


class Reader:
    """A stand-in for Claude that also reports the venue's opening hours."""
    def __init__(self, events, hours):
        self.reading = PageReading(events=events, venue_hours=hours)
        self.messages = SimpleNamespace(parse=lambda **kw: SimpleNamespace(
            stop_reason="end_turn", parsed_output=self.reading, usage=SimpleNamespace(input_tokens=10, output_tokens=5)))


def show(title, *dated):
    return FoundEvent(title=title, url=f"https://goodman.example/{title.lower()}", kind="production", category="Theater",
                      performances=[Performance(date=d, time=t) for d, t in dated])


# Wednesdays 2026-11-04 and 11-11 at 19:30; one Sunday matinee 11-08 at 14:00; a past Wednesday
RUN = show("Hamlet", ("2026-11-04", "19:30"), ("2026-11-11", "19:30"), ("2026-11-08", "14:00"), ("2026-10-07", "19:30"))


def test_admins_set_and_clear_hours(client, admin_token, user_token, db, make_event):
    make_event("Hamlet")
    v = db.scalar(select(Venue))
    h = auth(admin_token)
    body = {"open_hours": HOURS, "show_times": {"wed": ["19:30"], "sun": ["14:00", "19:30"]}}
    r = client.patch(f"/venues/{v.id}", json=body, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["open_hours"], out["open_hours_by"]) == (HOURS, "admin")
    assert (out["show_times"], out["show_times_by"]) == ({"wed": ["19:30"], "sun": ["14:00", "19:30"]}, "admin")
    listed = {x["id"]: x for x in client.get("/venues/manage", headers=h).json()}[v.id]
    assert listed["open_hours"] == HOURS
    for bad in ({"open_hours": {"mon": ["9am", "5pm"]}}, {"open_hours": {"funday": None}},
                {"show_times": {"wed": ["25:00"]}}, {"show_times": {"wed": ["19:30"] * 7}}):
        assert client.patch(f"/venues/{v.id}", json=bad, headers=h).status_code == 422, bad
    r = client.patch(f"/venues/{v.id}", json={"open_hours": {}}, headers=h).json()
    assert (r["open_hours"], r["open_hours_by"], r["show_times_by"]) == ({}, "", "admin")  # cleared one, kept the other
    assert client.patch(f"/venues/{v.id}", json=body, headers=auth(user_token)).status_code == 403
    assert "open_hours" not in client.get("/events").json()["items"][0]["venue"]  # stored, not shown on cards


def test_show_times_need_two_upcoming_dates():
    items = [SimpleNamespace(performances=[p.model_dump() for p in RUN.performances])]
    assert show_times_from(items, date(2026, 10, 20)) == {"wed": ["19:30"]}


def test_discovery_fills_hours_but_never_replaces_an_admins(db, pages):  # noqa: F811
    served, _ = pages
    served["https://goodman.example/season"] = {"html": "<p>season</p>"}
    s = make_source(db)
    hours = [OpeningHours(weekday="mon", open="10:00", close="17:00"), OpeningHours(weekday="tue")]
    out = runner.run(db, Reader([RUN], hours), TODAY)[0][1]
    venue = db.get(Venue, s.venue_id)
    assert venue.open_hours == {"mon": ["10:00", "17:00"], "tue": None} and venue.open_hours_by == "discovery"
    assert venue.show_times == {"wed": ["19:30"]} and venue.show_times_by == "discovery"
    assert "venue opening hours filled in" in out.notes
    # an admin's version stands, even when the page says something else next time
    venue.open_hours, venue.open_hours_by = {"mon": ["09:00", "18:00"]}, "admin"
    venue.show_times, venue.show_times_by = {"fri": ["20:00"]}, "admin"
    served["https://goodman.example/season"] = {"html": "<p>season, changed</p>"}
    db.commit()
    runner.run(db, Reader([RUN], hours), TODAY, force=True)
    db.refresh(venue)
    assert venue.open_hours == {"mon": ["09:00", "18:00"]} and venue.show_times == {"fri": ["20:00"]}
    assert db.scalar(select(DiscoveredItem)).title == "Hamlet"


def test_merging_keeps_the_hours(client, admin_token, db, make_event):
    make_event("Hamlet", venue_name="Goodman", neighborhood="Loop")
    old = db.scalar(select(Venue).where(Venue.name == "Goodman"))
    new = Venue(name="Goodman Theatre", neighborhood="Loop"); db.add(new)
    old.open_hours, old.open_hours_by = HOURS, "admin"; db.commit()
    assert client.post(f"/venues/{old.id}/merge-into/{new.id}", headers=auth(admin_token)).status_code == 200
    db.expire_all()
    assert (db.get(Venue, new.id).open_hours, db.get(Venue, new.id).open_hours_by) == (HOURS, "admin")


def test_a_source_is_linked_once_its_venue_exists(db):
    from app.discovery.hours import link_venue
    from app.models import DiscoverySource, SourceKind
    s = DiscoverySource(name="Field", kind=SourceKind.museum, pages=[], reader="claude", venue_name="The Field Museum",
                        every_days=30, page_state={}, venue_hours={}, unchanged_streak=0, last_status="", last_error="")
    db.add(s); db.commit()
    assert not link_venue(db, s)  # no such venue yet
    v = Venue(name="Field Museum", neighborhood="South Loop"); db.add(v); db.commit()
    assert link_venue(db, s) and s.venue_id == v.id
    other = DiscoverySource(name="Second City", kind=SourceKind.comedy, pages=[], reader="claude", venue_name="The Second City",
                            every_days=14, page_state={}, venue_hours={}, unchanged_streak=0, last_status="", last_error="")
    db.add_all([other, Venue(name="The Second City", neighborhood="Old Town"), Venue(name="The Second City", neighborhood="Gold Coast")])
    db.commit()
    assert not link_venue(db, other) and other.venue_id is None  # two places by that name: an admin decides
