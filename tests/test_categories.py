"""CC-125: the five categories, each venue's default, and opera (Music, also listed under Theater)."""
from sqlalchemy import select

from app.categories import fill_venue_defaults
from app.discovery.jsonld import BY_SOURCE
from app.discovery.store import record
from app.models import DiscoveredItem, Event, SourceKind, Venue
from app.seo import description

from .conftest import auth
from .test_discovery import make_source
from .test_discovery_review import found
from .test_events import titles


def venue(db, name="Art Place", category=None, opera=False):
    v = Venue(name=name, neighborhood="Loop", category=category, opera=opera)
    db.add(v); db.commit()
    return v


def test_a_suggestion_starts_with_the_venues_default_and_can_differ(client, user_token, db):
    v = venue(db, category="Visual Art")
    r = client.post("/events", json={"title": "Show", "venue_id": v.id}, headers=auth(user_token))
    assert r.status_code == 201 and r.json()["category"] == "Visual Art"
    r = client.post("/events", json={"title": "Talk", "category": "Educational", "venue_id": v.id}, headers=auth(user_token))
    assert r.status_code == 201 and r.json()["category"] == "Educational"
    # no default and no category: nothing to start with
    bare = venue(db, name="Bare")
    assert client.post("/events", json={"title": "X", "venue_id": bare.id}, headers=auth(user_token)).status_code == 422


def test_a_new_venue_takes_its_first_events_category(client, user_token, db):
    body = {"title": "Set", "category": "Comedy", "venue": {"name": "New Club", "neighborhood": "Lakeview"}}
    r = client.post("/events", json=body, headers=auth(user_token))
    assert r.status_code == 201 and r.json()["venue"]["category"] == "Comedy"


def test_unknown_categories_are_rejected(client, user_token, admin_token, make_event):
    ev = make_event("Hamlet")
    body = {"title": "Gig", "category": "Concert", "venue_id": ev.venue_id}
    assert client.post("/events", json=body, headers=auth(user_token)).status_code == 422
    assert client.patch(f"/events/{ev.id}", json={"category": "Dance"}, headers=auth(admin_token)).status_code == 422
    assert client.patch(f"/venues/{ev.venue_id}", json={"category": "Dance"}, headers=auth(admin_token)).status_code == 422


def test_changing_a_venues_default_leaves_its_events_alone(client, admin_token, make_event, db):
    ev = make_event("Hamlet")
    r = client.patch(f"/venues/{ev.venue_id}", json={"category": "Comedy"}, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["category"] == "Comedy" and r.json()["opera"] is False
    db.refresh(ev)
    assert ev.category == "Theater"
    listed = {v["id"]: v for v in client.get("/venues/manage", headers=auth(admin_token)).json()}
    assert listed[ev.venue_id]["category"] == "Comedy"
    # null clears it
    assert client.patch(f"/venues/{ev.venue_id}", json={"category": None}, headers=auth(admin_token)).json()["category"] is None


def test_venue_opera_default_is_music(client, admin_token, user_token, db):
    v = venue(db, name="Lyric Opera", category="Theater")
    h = auth(admin_token)
    r = client.patch(f"/venues/{v.id}", json={"opera": True}, headers=h).json()
    assert (r["category"], r["opera"]) == ("Music", True)
    assert client.patch(f"/venues/{v.id}", json={"category": "Comedy", "opera": True}, headers=h).status_code == 422
    # a member's suggestion there starts as opera, though they can't choose it
    r = client.post("/events", json={"title": "Tosca", "venue_id": v.id}, headers=auth(user_token))
    assert r.status_code == 201 and (r.json()["category"], r.json()["opera"]) == ("Music", True)
    r = client.post("/events", json={"title": "Gala", "category": "Theater", "venue_id": v.id}, headers=auth(user_token))
    assert r.json()["opera"] is False  # not Music, so not an opera
    # moving the default off Music drops opera
    assert client.patch(f"/venues/{v.id}", json={"category": "Theater"}, headers=h).json()["opera"] is False


def test_only_admins_mark_opera(client, user_token, admin_token, make_event):
    ev = make_event("Hamlet")
    body = {"title": "Carmen", "category": "Music", "opera": True, "venue_id": ev.venue_id}
    assert client.post("/events", json=body, headers=auth(user_token)).status_code == 403
    r = client.post("/events", json=body, headers=auth(admin_token))
    assert r.status_code == 201 and r.json()["opera"] is True
    assert client.post("/events", json={**body, "category": "Comedy"}, headers=auth(admin_token)).status_code == 422
    assert client.patch(f"/events/{ev.id}", json={"opera": True}, headers=auth(user_token)).status_code == 403
    # the editor: ticking opera makes it Music; another category clears it
    r = client.patch(f"/events/{ev.id}", json={"opera": True}, headers=auth(admin_token)).json()
    assert (r["category"], r["opera"]) == ("Music", True)
    r = client.patch(f"/events/{ev.id}", json={"category": "Theater"}, headers=auth(admin_token)).json()
    assert (r["category"], r["opera"]) == ("Theater", False)
    assert client.patch(f"/events/{ev.id}", json={"opera": True, "category": "Comedy"}, headers=auth(admin_token)).status_code == 422


def test_an_opera_is_under_music_and_theater_once(client, make_event, db):
    tosca = make_event("Tosca", venue_name="Lyric Opera", category="Music")
    tosca.opera = True; db.commit()
    make_event("Hamlet")
    make_event("Gig", venue_name="Metro", category="Music")
    assert sorted(titles(client.get("/events?category=Music"))) == ["Gig", "Tosca"]
    assert sorted(titles(client.get("/events?category=Theater"))) == ["Hamlet", "Tosca"]
    both = titles(client.get("/events?category=Theater&category=Music"))
    assert sorted(both) == ["Gig", "Hamlet", "Tosca"]  # Tosca once
    assert client.get("/events?category=Theater&category=Music").json()["total"] == 3
    assert description(tosca, tosca.begin_date).startswith("Opera at Lyric Opera")


def test_stats_count_an_opera_once_under_music(client, admin_token, user_token, make_event, db):
    tosca = make_event("Tosca", venue_name="Lyric Opera", category="Music")
    tosca.opera = True; db.commit()
    assert client.put(f"/me/plans/{tosca.id}", json={"status": "interested"}, headers=auth(user_token)).status_code == 201
    music = client.get("/admin/stats/events?range=all&category=Music", headers=auth(admin_token)).json()
    theater = client.get("/admin/stats/events?range=all&category=Theater", headers=auth(admin_token)).json()
    assert [r["event"]["title"] for r in music] == ["Tosca"] and theater == []


def test_admin_sees_events_outside_the_five(client, admin_token, user_token, make_event):
    make_event("Hamlet")
    make_event("Old gig", venue_name="Metro", category="Concert")  # as the migration would leave something it can't map
    r = client.get("/admin/events/other-types", headers=auth(admin_token))
    assert [e["title"] for e in r.json()] == ["Old gig"]
    assert client.get("/admin/events/other-types", headers=auth(user_token)).status_code == 403


def test_fill_venue_defaults_uses_the_most_common_category(db, make_event):
    make_event("A", venue_name="Mixed", category="Comedy")
    make_event("B", venue_name="Mixed", category="Music")
    make_event("C", venue_name="Mixed", category="Music")
    make_event("D", venue_name="Odd", category="Concert")  # not one of the five: no default from it
    set_already = venue(db, name="Set", category="Theater")
    fill_venue_defaults(db)
    by = {v.name: v.category for v in db.scalars(select(Venue))}
    assert by["Mixed"] == "Music" and by["Odd"] is None and by["Set"] == "Theater"
    assert set_already.category == "Theater"


def test_discovery_kinds_map_to_the_five(db):
    assert BY_SOURCE["music"][1] == "Music" and BY_SOURCE["museum"][1] == "Visual Art" and BY_SOURCE["opera"][1] == "Music"
    opera = make_source(db)
    opera.kind = SourceKind.opera; db.commit()
    record(db, opera, found("Tosca", "https://lyric.example/tosca", category="Theater"), ""); db.commit()
    item = db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == "Tosca"))
    assert (item.category, item.opera) == ("Music", True)


def test_found_items_offer_the_venues_default_and_approve_with_an_override(client, admin_token, db):
    s = make_source(db)
    s.venue.category = "Visual Art"; db.commit()
    record(db, s, found("Curator talk", "https://goodman.example/talk", kind="talk", category=None, begin_date="2027-01-10"), "")
    db.commit()
    item = db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == "Curator talk"))
    out = next(i for i in client.get("/admin/discovery/items", headers=auth(admin_token)).json()["items"] if i["id"] == item.id)
    assert (out["category"], out["venue_category"], out["venue_opera"]) == ("Theater", "Visual Art", False)
    body = {"title": "Curator talk", "category": "Educational", "venue_id": s.venue_id, "begin_date": "2027-01-10", "publish": "now"}
    r = client.post(f"/admin/discovery/items/{item.id}/approve", json=body, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert db.get(Event, r.json()["event"]["id"]).category == "Educational"
