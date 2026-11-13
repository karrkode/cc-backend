"""CC-217: the site covers Chicago and its suburbs. Nothing new can be out of town, discovery leaves it out, and the
admin clean-up lists what's left. Its deletes are the usual DELETE /events/<id> and DELETE /venues/<id>."""
from sqlalchemy import func, select

from app.discovery.readers import FoundEvent
from app.discovery.store import record
from app.models import Area, DiscoveredItem, DiscoverySource, Event, Plan, PlanStatus, Region, SourceKind, User, Venue

from .conftest import auth


def far_venue(db, name="Racine Art Museum", hood="Racine, WI"):
    v = Venue(name=name, neighborhood=hood, area=Area.out_of_town, region=Region.out_of_town)
    db.add(v); db.commit()
    return v


def source_at(db, venue, name="Racine shows", active=True):
    s = DiscoverySource(name=name, kind=SourceKind.museum, pages=["https://racine.example/"], reader="claude", venue_id=venue.id,
                        venue_name=venue.name, neighborhood=venue.neighborhood, every_days=30, page_state={}, venue_hours={},
                        unchanged_streak=0, last_status="", last_error="", active=active)
    db.add(s); db.commit()
    return s


def overview(client, token):
    r = client.get("/admin/out-of-town", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


# ---- nothing new is out of town ------------------------------------------------------------

def test_a_new_venue_cant_be_out_of_town(client, admin_token, db):
    h = auth(admin_token)
    assert client.post("/venues/from-event", json={"name": "Museum", "neighborhood": "Evanston, IL", "area": "out_of_town"}, headers=h).status_code == 422
    r = client.post("/venues/from-event", json={"name": "Museum", "neighborhood": "Milwaukee, WI"}, headers=h)  # worked out from the place
    assert r.status_code == 422 and "Chicago and its suburbs" in r.json()["detail"]
    assert client.post("/venues/from-event", json={"name": "Museum", "neighborhood": "Rockford, IL"}, headers=h).status_code == 422
    assert db.scalar(select(func.count(Venue.id))) == 0
    r = client.post("/venues/from-event", json={"name": "Museum", "neighborhood": "Evanston, IL"}, headers=h)
    assert r.status_code == 201 and r.json()["area"] == "suburbs"
    # an admin who knows better can still choose: this one is just over the state line
    r = client.post("/venues/from-event", json={"name": "Theatre", "neighborhood": "Hammond, IN", "area": "suburbs"}, headers=h)
    assert r.status_code == 201 and (r.json()["area"], r.json()["region"]) == ("suburbs", "suburbs")


def test_an_edited_venue_cant_become_out_of_town(client, admin_token, make_event, db):
    ev = make_event("Hamlet")
    url, h = f"/venues/{ev.venue_id}", auth(admin_token)
    assert client.patch(url, json={"area": "out_of_town"}, headers=h).status_code == 422
    assert client.patch(url, json={"region": "out_of_town"}, headers=h).status_code == 422
    assert client.patch(url, json={"neighborhood": "Racine, WI"}, headers=h).status_code == 422
    db.expire_all()
    v = db.get(Venue, ev.venue_id)
    assert (v.area, v.neighborhood) == (Area.city, "Loop")


def test_an_old_out_of_town_venue_can_still_be_read_renamed_and_brought_in(client, admin_token, make_event, db):
    v = far_venue(db)
    h = auth(admin_token)
    ev = make_event("Jewelry", venue_name=v.name, neighborhood=v.neighborhood)
    assert client.get(f"/events/{ev.id}").json()["venue"]["area"] == "out_of_town"
    r = client.patch(f"/venues/{v.id}", json={"name": "Racine Art"}, headers=h)
    assert r.status_code == 200 and r.json()["area"] == "out_of_town"
    r = client.patch(f"/venues/{v.id}", json={"area": "suburbs"}, headers=h)
    assert (r.json()["area"], r.json()["region"]) == ("suburbs", "suburbs")


def test_a_submission_cant_add_an_out_of_town_venue(client, user_token, db):
    body = {"title": "Road Trip", "category": "Theater", "begin_date": "2030-01-01", "end_date": "2030-01-05",
            "url": "https://example.org/trip", "venue": {"name": "Pabst Theater", "neighborhood": "Milwaukee, WI"}}
    r = client.post("/events", json=body, headers=auth(user_token))
    assert r.status_code == 422 and "Chicago and its suburbs" in r.json()["detail"]
    assert db.scalar(select(func.count(Event.id))) == 0 and db.scalar(select(func.count(Venue.id))) == 0


# ---- discovery ---------------------------------------------------------------------------

def test_discovery_leaves_out_events_at_out_of_town_venues(db):
    far = far_venue(db)
    city = Venue(name="Goodman Theatre", neighborhood="Loop")
    suburb = Venue(name="Writers Theatre", neighborhood="Glencoe, IL", area=Area.suburbs, region=Region.suburbs)
    db.add_all([city, suburb]); db.commit()
    s = source_at(db, far)
    assert record(db, s, FoundEvent(title="Jewelry", url="https://racine.example/j", kind="exhibition"), "") == "skipped"
    # a source elsewhere: the venue the item names decides it
    near = source_at(db, city, name="Goodman")
    assert record(db, near, FoundEvent(title="On tour", url="https://g.example/t", kind="production", venue_name="Racine Art Museum"), "") == "skipped"
    assert record(db, near, FoundEvent(title="Hamlet", url="https://g.example/h", kind="production"), "") == "new"
    assert record(db, near, FoundEvent(title="Glencoe run", url="https://g.example/g", kind="production", venue_name="Writers Theatre"), "") == "new"
    db.commit()
    got = {i.title: (i.status, i.skip_reason) for i in db.scalars(select(DiscoveredItem))}
    assert got == {"Jewelry": ("skipped", "out of town"), "On tour": ("skipped", "out of town"),
                   "Hamlet": ("new", ""), "Glencoe run": ("new", "")}


def test_a_waiting_item_is_left_out_when_seen_again_once_its_venue_is_out_of_town(db):
    v = Venue(name="Racine Art Museum", neighborhood="Racine, WI", area=Area.suburbs, region=Region.suburbs)
    db.add(v); db.commit()
    s = source_at(db, v)
    found = FoundEvent(title="Jewelry", url="https://racine.example/j", kind="exhibition")
    assert record(db, s, found, "") == "new"
    v.area = Area.out_of_town
    db.commit()
    assert record(db, s, found, "") == "skipped"


# ---- the clean-up ------------------------------------------------------------------------

def test_the_clean_up_lists_only_out_of_town_venues_with_their_counts(client, admin_token, make_event, db):
    far, empty = far_venue(db), far_venue(db, "Pabst Theater", "Milwaukee, WI")
    a = make_event("Jewelry", venue_name=far.name, neighborhood=far.neighborhood)
    b = make_event("Pending one", status="pending", venue_name=far.name, neighborhood=far.neighborhood)
    make_event("Hamlet")
    make_event("Glencoe run", venue_name="Writers Theatre", neighborhood="Glencoe, IL", area=Area.suburbs)
    on, off = source_at(db, far), source_at(db, far, name="Old source", active=False)
    db.add(DiscoveredItem(source_id=on.id, key="k", title="Waiting", first_seen_at=a.created_at, last_seen_at=a.created_at,
                          changes={}, detail_checks=[], status="new"))
    db.commit()
    got = overview(client, admin_token)
    assert got["event_ids"] == [a.id, b.id] and got["waiting"] == 1
    assert [(v["name"], v["events"], [s["name"] for s in v["sources"]]) for v in got["venues"]] == \
        [("Pabst Theater", 0, []), ("Racine Art Museum", 2, ["Racine shows"])]
    assert off.id not in [s["id"] for v in got["venues"] for s in v["sources"]]


def test_a_venue_marked_out_of_town_by_side_of_town_only_is_listed(client, admin_token, db):
    v = Venue(name="Border Hall", neighborhood="Kenosha, WI", area=Area.suburbs, region=Region.out_of_town)
    db.add(v); db.commit()
    assert [x["id"] for x in overview(client, admin_token)["venues"]] == [v.id]


def test_the_clean_up_is_for_admins(client, user_token):
    assert client.get("/admin/out-of-town").status_code == 401
    assert client.get("/admin/out-of-town", headers=auth(user_token)).status_code == 403


def test_running_the_clean_up_leaves_city_and_suburbs_alone(client, admin_token, make_event, db):
    h = auth(admin_token)
    far = far_venue(db)
    gone = make_event("Jewelry", venue_name=far.name, neighborhood=far.neighborhood)
    city = make_event("Hamlet")
    suburb = make_event("Glencoe run", venue_name="Writers Theatre", neighborhood="Glencoe, IL", area=Area.suburbs)
    user = db.scalar(select(User))
    db.add_all([Plan(user_id=user.id, event_id=gone.id, status=PlanStatus.interested),
                Plan(user_id=user.id, event_id=city.id, status=PlanStatus.interested)])
    db.commit()
    s = source_at(db, far)
    keep = source_at(db, db.get(Venue, city.venue_id), name="Goodman")

    # a venue with events can't go first
    assert client.delete(f"/venues/{far.id}", headers=h).status_code == 409
    # what the page does: sources off, each event by the admin delete, then the empty venue
    for v in overview(client, admin_token)["venues"]:
        for src in v["sources"]:
            assert client.patch(f"/admin/discovery/sources/{src['id']}", json={"active": False}, headers=h).status_code == 200
    for eid in overview(client, admin_token)["event_ids"]:
        assert client.delete(f"/events/{eid}?live=true", headers=h).status_code == 204
    left = overview(client, admin_token)
    assert left["event_ids"] == [] and [v["events"] for v in left["venues"]] == [0]
    assert client.delete(f"/venues/{far.id}", headers=h).status_code == 204

    assert overview(client, admin_token) == {"venues": [], "event_ids": [], "waiting": 0}
    db.expire_all()
    assert {e.title for e in db.scalars(select(Event))} == {"Hamlet", "Glencoe run"}
    assert {v.name for v in db.scalars(select(Venue))} == {"Goodman Theatre", "Writers Theatre"}
    assert [p.event_id for p in db.scalars(select(Plan))] == [city.id]  # its plans went with it, others stay
    assert db.get(DiscoverySource, s.id).active is False and db.get(DiscoverySource, keep.id).active is True
    assert suburb.id in [e["id"] for e in client.get("/events").json()["items"]]
