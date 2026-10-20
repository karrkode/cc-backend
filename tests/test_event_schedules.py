"""CC-79: events' schedules for the planner: found dates and times, a weekly pattern, the venue's hours."""
from datetime import date, timedelta

from sqlalchemy import select

from app.discovery.readers import FoundEvent, Performance
from app.discovery.store import record
from app.models import DiscoveredItem, Event, Venue

from .conftest import auth
from .test_discovery import make_source


def schedule_of(client, event_id):
    return client.get(f"/events/{event_id}").json()["schedule"]


def test_the_schedule_comes_from_the_event_then_its_venue(client, admin_token, db, make_event):
    ev = make_event("Hamlet")
    venue = db.get(Venue, ev.venue_id)
    venue.show_times, venue.open_hours = {"thu": ["19:30"]}, {"mon": None}
    soon, past = (date.today() + timedelta(days=3)).isoformat(), (date.today() - timedelta(days=3)).isoformat()
    ev.performances = [{"date": past, "time": "19:30"}, {"date": soon, "time": "14:00"}]
    db.commit()
    s = schedule_of(client, ev.id)
    assert s == {"dates": [{"date": soon, "time": "14:00"}], "weekly": {"thu": ["19:30"]}, "weekly_from": "venue", "hours": {"mon": None}}
    h = auth(admin_token)
    assert client.patch(f"/events/{ev.id}", json={"weekly": {"sat": ["20:00"]}}, headers=h).status_code == 200
    s = schedule_of(client, ev.id)
    assert (s["weekly"], s["weekly_from"]) == ({"sat": ["20:00"]}, "event")  # the event's own beats the venue's
    assert client.patch(f"/events/{ev.id}", json={"weekly": {"sat": ["8pm"]}}, headers=h).status_code == 422
    assert client.patch(f"/events/{ev.id}", json={"weekly": None}, headers=h).status_code == 422
    assert client.patch(f"/events/{ev.id}", json={"weekly": {}}, headers=h).status_code == 200
    assert schedule_of(client, ev.id)["weekly_from"] == "venue"
    assert "schedule" in client.get("/events").json()["items"][0]


def test_discovery_passes_dates_on_to_listed_and_approved_events(client, admin_token, db, make_event):
    s = make_source(db)
    listed = make_event("Hamlet", venue_name="Goodman Theatre", neighborhood="Loop")
    db.commit()
    later = (date.today() + timedelta(days=20)).isoformat()
    shows = [Performance(date=later, time="19:30"), Performance(date=later, time="14:00"), Performance(date=later, time="19:30")]
    record(db, s, FoundEvent(title="Hamlet", kind="production", category="Theater", performances=shows), "")
    db.commit(); db.refresh(listed)
    assert listed.performances == [{"date": later, "time": "14:00"}, {"date": later, "time": "19:30"}]  # sorted, no repeats
    # approving a found event copies its dates too
    record(db, s, FoundEvent(title="Macbeth", url="https://goodman.example/macbeth", kind="production", category="Theater",
                             performances=[Performance(date=later, time="20:00")]), "")
    db.commit()
    item = db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == "Macbeth"))
    body = {"title": "Macbeth", "category": "Theater", "venue_id": s.venue_id, "url": item.url, "publish": "now"}
    r = client.post(f"/admin/discovery/items/{item.id}/approve", json=body, headers=auth(admin_token))
    db.expire_all()
    assert db.get(Event, r.json()["event"]["id"]).performances == [{"date": later, "time": "20:00"}]
