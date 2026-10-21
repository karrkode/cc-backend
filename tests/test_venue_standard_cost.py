"""CC-89: a venue's standard cost stands in for its events' price when they don't post one."""
from decimal import Decimal

from app.models import Venue

from .conftest import auth


def titles(r):
    return [e["title"] for e in r.json()["items"]]


def test_a_venues_standard_cost_is_a_fallback_for_its_events(client, admin_token, user_token, db, make_event):
    museum = make_event("Free Museum Show", venue_name="Free Museum", neighborhood="Loop")
    make_event("Free Museum Gala", venue_name="Free Museum", neighborhood="Loop", min_price=Decimal("150"), cost_text="$150")
    make_event("Club Night", venue_name="Club", neighborhood="Pilsen", min_price=Decimal("20"), cost_text="$20")
    make_event("Unpriced Elsewhere", venue_name="Elsewhere", neighborhood="Uptown")
    h = auth(admin_token)
    assert titles(client.get("/events?max_price=0")) == []

    r = client.patch(f"/venues/{museum.venue_id}", json={"standard_cost": "Free", "standard_min_price": 0}, headers=h)
    assert r.status_code == 200 and (r.json()["standard_cost"], r.json()["standard_min_price"]) == ("Free", 0)
    shown = client.get(f"/events/{museum.id}").json()
    assert shown["cost_text"] == "" and shown["min_price"] is None  # never copied into the event
    assert (shown["venue"]["standard_cost"], shown["venue"]["standard_min_price"]) == ("Free", 0)

    assert titles(client.get("/events?max_price=0")) == ["Free Museum Show"]  # its own price wins for the gala
    assert titles(client.get("/events?max_price=40&sort=price")) == ["Free Museum Show", "Club Night"]
    assert titles(client.get("/events?sort=price"))[-1] == "Unpriced Elsewhere"  # no price anywhere: last

    # cleared: gone from those events right away
    r = client.patch(f"/venues/{museum.venue_id}", json={"standard_cost": ""}, headers=h)
    assert (r.json()["standard_cost"], r.json()["standard_min_price"]) == ("", None)
    assert titles(client.get("/events?max_price=0")) == []

    assert client.patch(f"/venues/{museum.venue_id}", json={"standard_cost": "$25", "standard_min_price": -1}, headers=h).status_code == 422
    assert client.patch(f"/venues/{museum.venue_id}", json={"standard_cost": "Free"}, headers=auth(user_token)).status_code == 403


def test_merging_keeps_a_standard_cost(client, admin_token, db, make_event):
    a = make_event("A", venue_name="Hall", neighborhood="Loop")
    b = make_event("B", venue_name="Hall Two", neighborhood="Loop")
    db.get(Venue, a.venue_id).standard_cost, db.get(Venue, a.venue_id).standard_min_price = "$10 suggested", Decimal("10")
    db.commit()
    r = client.post(f"/venues/{a.venue_id}/merge-into/{b.venue_id}", headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["into"]["standard_cost"] == "$10 suggested"
