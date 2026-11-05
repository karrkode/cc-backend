"""CC-188: a venue from a member's submission waits for an admin before it's in the public list."""
from sqlalchemy import select

from app.discovery.store import venue_for
from app.models import DiscoveredItem, Venue

from .conftest import auth, register
from .test_categories import venue
from .test_discovery import make_source


def _submit(client, token, title="New Play", name="Tiny Stage", hood="Oak Park, IL", **body):
    where = {"venue": {"name": name, "neighborhood": hood}} if "venue_id" not in body else {}
    r = client.post("/events", json={"title": title, "category": "Theater", **where, **body}, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _public(client, headers=None):
    return [v["name"] for v in client.get("/venues", headers=headers).json()]


def _venue_row(db, name="Tiny Stage"):
    db.expire_all()
    return db.scalar(select(Venue).where(Venue.name == name))


def test_a_members_new_venue_stays_out_of_the_public_list(client, user_token, admin_token, db):
    made = _submit(client, user_token)
    assert _venue_row(db).pending is True
    assert "Tiny Stage" not in _public(client)
    assert "Tiny Stage" not in _public(client, auth(user_token))
    assert client.get("/venues?q=tiny").json() == []
    # the admin reviewing the event gets it, in the list the event editor looks venues up in
    assert "Tiny Stage" in _public(client, auth(admin_token))
    queue = client.get("/admin/events", headers=auth(admin_token)).json()["items"]
    assert [e["venue"]["name"] for e in queue if e["id"] == made["id"]] == ["Tiny Stage"]
    assert "Tiny Stage" in [v["name"] for v in client.get("/venues/manage", headers=auth(admin_token)).json()]


def test_a_second_submission_reuses_the_pending_venue(client, user_token, db):
    first, second = _submit(client, user_token), _submit(client, user_token, title="Other Play", name="tiny stage", hood="oak park, il")
    assert first["venue"]["id"] == second["venue"]["id"]


def test_a_member_cant_name_a_pending_venue_by_id(client, user_token, admin_token, db):
    pending_id = _submit(client, user_token)["venue"]["id"]
    other = register(client, "bo@example.com", name="Bo")
    r = client.post("/events", json={"title": "X", "category": "Theater", "venue_id": pending_id}, headers=auth(other))
    assert r.status_code == 422 and "Tiny Stage" not in r.text
    assert _submit(client, admin_token, venue_id=pending_id)["status"] == "pending"  # an admin can


def test_an_admins_new_venue_is_live_at_once(client, admin_token, db):
    _submit(client, admin_token)
    assert _venue_row(db).pending is False
    assert "Tiny Stage" in _public(client)
    assert _venue_row(db).category is None  # the event waits for review, so it sets no default yet
    _submit(client, admin_token, title="Now", publish="now")
    assert _venue_row(db).category == "Theater"


def test_approving_makes_the_venue_live_and_sets_its_default(client, user_token, admin_token, db):
    event = _submit(client, user_token)
    assert client.post(f"/admin/events/{event['id']}/approve", headers=auth(admin_token)).status_code == 200
    v = _venue_row(db)
    assert v.pending is False and v.category == "Theater"
    assert "Tiny Stage" in _public(client)


def test_approving_through_the_edit_form_counts_too(client, user_token, admin_token, db):
    event = _submit(client, user_token)
    r = client.patch(f"/events/{event['id']}", json={"status": "approved"}, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    v = _venue_row(db)
    assert v.pending is False and v.category == "Theater"


def test_a_pending_submission_leaves_an_existing_venues_default_alone(client, user_token, admin_token, db):
    v = venue(db, name="Bare Hall")
    event = _submit(client, user_token, venue_id=v.id)
    assert _venue_row(db, "Bare Hall").category is None
    client.post(f"/admin/events/{event['id']}/reject", headers=auth(admin_token))
    assert _venue_row(db, "Bare Hall").category is None
    client.post(f"/admin/events/{event['id']}/approve", headers=auth(admin_token))
    assert _venue_row(db, "Bare Hall").category == "Theater"


def test_editing_a_live_event_doesnt_refill_a_cleared_default(client, admin_token, db):
    event = _submit(client, admin_token, publish="now")
    client.patch(f"/venues/{event['venue']['id']}", json={"category": None}, headers=auth(admin_token))
    client.patch(f"/events/{event['id']}", json={"title": "Renamed"}, headers=auth(admin_token))
    assert _venue_row(db).category is None


def test_a_rejected_event_keeps_its_venue_hidden_and_can_be_approved_after_all(client, user_token, admin_token, db):
    event = _submit(client, user_token)
    assert client.post(f"/admin/events/{event['id']}/reject", headers=auth(admin_token)).status_code == 200
    assert _venue_row(db).pending is True and "Tiny Stage" not in _public(client)
    sent = client.get("/me/submissions", headers=auth(user_token)).json()
    assert [e["venue"]["name"] for e in sent] == ["Tiny Stage"]  # still on the member's list
    assert client.post(f"/admin/events/{event['id']}/approve", headers=auth(admin_token)).status_code == 200
    assert _venue_row(db).pending is False and "Tiny Stage" in _public(client)


def test_an_auto_rejected_event_leaves_its_venue_hidden(client, user_token, admin_token, db):
    client.post("/admin/submission-rules", json={"kind": "name", "phrase": "spam"}, headers=auth(admin_token))
    event = _submit(client, user_token, title="Spam Night")
    assert event["status"] == "rejected"
    v = _venue_row(db)
    assert v.pending is True and v.category is None
    assert "Tiny Stage" not in _public(client)
    client.post(f"/admin/events/{event['id']}/approve", headers=auth(admin_token))  # the rule was wrong
    assert "Tiny Stage" in _public(client)


def test_deleting_the_last_event_removes_a_pending_venue(client, user_token, admin_token, db):
    one, two = _submit(client, user_token), _submit(client, user_token, title="Two")
    client.post(f"/admin/events/{one['id']}/reject", headers=auth(admin_token))
    assert client.delete(f"/events/{one['id']}", headers=auth(admin_token)).status_code == 204
    assert _venue_row(db) is not None  # the other event still uses it
    assert client.delete(f"/events/{two['id']}", headers=auth(admin_token)).status_code == 204
    assert _venue_row(db) is None


def test_moving_the_last_event_removes_a_pending_venue(client, user_token, admin_token, db):
    event = _submit(client, user_token)
    real = venue(db, name="Real Hall", category="Theater")
    r = client.patch(f"/events/{event['id']}", json={"venue_id": real.id}, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert _venue_row(db) is None


def test_an_auto_reject_rule_keeps_a_pending_venue(client, user_token, admin_token, db):
    event = _submit(client, user_token)
    r = client.post("/admin/submission-rules", json={"kind": "venue", "venue_id": event["venue"]["id"]}, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    client.delete(f"/events/{event['id']}", headers=auth(admin_token))
    assert _venue_row(db) is not None


def test_a_discovery_source_keeps_a_pending_venue(client, user_token, admin_token, db):
    event = _submit(client, user_token)
    source = make_source(db)
    source.venue_id = event["venue"]["id"]
    db.commit()
    client.delete(f"/events/{event['id']}", headers=auth(admin_token))
    assert _venue_row(db) is not None


def test_deleting_an_event_never_removes_a_live_venue(client, admin_token, db):
    event = _submit(client, admin_token, publish="now")
    client.delete(f"/events/{event['id']}", headers=auth(admin_token))
    assert _venue_row(db) is not None


def test_merging_a_live_venue_into_a_pending_one_makes_it_live(client, user_token, admin_token, db):
    pending_id = _submit(client, user_token)["venue"]["id"]
    live = venue(db, name="Real Hall", category="Theater")
    assert client.post(f"/venues/{live.id}/merge-into/{pending_id}", headers=auth(admin_token)).status_code == 200
    assert _venue_row(db).pending is False


def test_discovery_doesnt_file_found_events_under_a_pending_venue(client, user_token, db):
    _submit(client, user_token)
    source = make_source(db)
    item = DiscoveredItem(source_id=source.id, title="Show", venue_name="Tiny Stage")
    assert venue_for(db, source, item) is None


def test_a_link_draft_doesnt_read_a_pending_venue(client, user_token, db):
    from datetime import date
    from app.discovery.readers import FoundEvent
    from app.fromlink import _draft
    _submit(client, user_token)
    draft = _draft(db, FoundEvent(title="Show", kind="production", venue_name="Tiny Stage"), "https://tiny.example/a", {}, "page", date(2026, 11, 5), 0)
    assert draft["neighborhood"] == "" and draft["category"] is None
