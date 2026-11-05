"""CC-219: an admin can take a venue out of the public venue list without losing anything."""
from sqlalchemy import select

from app.models import Event, Venue

from .conftest import auth
from .test_categories import venue
from .test_pending_venues import _public, _submit, _venue_row


def _hide(client, token, venue_id, action="hide"):
    r = client.post(f"/venues/{venue_id}/{action}", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _live_event(client, admin_token, venue_id, title="Long Run"):
    r = client.post("/events", json={"title": title, "category": "Theater", "venue_id": venue_id, "publish": "now"},
                    headers=auth(admin_token))
    assert r.status_code == 201, r.text
    return r.json()


def test_a_hidden_venue_leaves_the_public_list_but_not_the_admins(client, db, user_token, admin_token):
    v = venue(db, "Old Hall")
    assert _hide(client, admin_token, v.id)["hidden"] is True
    assert "Old Hall" not in _public(client)
    assert "Old Hall" not in _public(client, auth(user_token))
    assert client.get("/venues?q=old", headers=auth(user_token)).json() == []
    assert "Old Hall" in _public(client, auth(admin_token))  # the event editor looks venues up here
    rows = client.get("/venues/manage", headers=auth(admin_token)).json()
    assert [r["hidden"] for r in rows if r["name"] == "Old Hall"] == [True]


def test_unhiding_puts_it_back_as_it_was(client, db, admin_token):
    v = venue(db, "Old Hall")
    client.patch(f"/venues/{v.id}", json={"website": "https://oldhall.example"}, headers=auth(admin_token))
    before = next(r for r in client.get("/venues/manage", headers=auth(admin_token)).json() if r["id"] == v.id)
    _hide(client, admin_token, v.id)
    assert _hide(client, admin_token, v.id, "unhide") == before
    assert "Old Hall" in _public(client)


def test_its_events_stay_listed_and_still_show_the_venue(client, db, admin_token):
    v = venue(db, "Old Hall")
    made = _live_event(client, admin_token, v.id)
    _hide(client, admin_token, v.id)
    assert client.get(f"/events/{made['id']}").json()["venue"]["name"] == "Old Hall"
    assert made["id"] in [e["id"] for e in client.get("/events").json()["items"]]
    assert db.scalar(select(Event.id).where(Event.venue_id == v.id)) == made["id"]


def test_hiding_takes_an_admin(client, db, user_token):
    v = venue(db, "Old Hall")
    assert client.post(f"/venues/{v.id}/hide").status_code == 401
    assert client.post(f"/venues/{v.id}/hide", headers=auth(user_token)).status_code == 403
    assert client.post(f"/venues/{v.id}/unhide", headers=auth(user_token)).status_code == 403


def test_hiding_an_unknown_venue_is_a_404(client, admin_token):
    assert client.post("/venues/999999/hide", headers=auth(admin_token)).status_code == 404


def test_a_member_naming_a_hidden_venue_attaches_and_it_stays_hidden(client, db, user_token, admin_token):
    v = venue(db, "Old Hall")
    _hide(client, admin_token, v.id)
    made = _submit(client, user_token, name="old hall", hood=v.neighborhood)
    assert made["venue"]["id"] == v.id
    assert client.post(f"/admin/events/{made['id']}/approve", headers=auth(admin_token)).status_code == 200
    assert _venue_row(db, "Old Hall").hidden is True
    assert "Old Hall" not in _public(client)


def test_the_same_name_in_another_neighborhood_is_a_new_pending_venue(client, db, user_token, admin_token):
    v = venue(db, "Old Hall")
    _hide(client, admin_token, v.id)
    made = _submit(client, user_token, name="Old Hall", hood="Evanston, IL")
    assert made["venue"]["id"] != v.id
    db.expire_all()
    assert db.get(Venue, made["venue"]["id"]).pending is True


def test_approving_an_event_ends_pending_but_not_hidden(client, user_token, admin_token, db):
    made = _submit(client, user_token)
    _hide(client, admin_token, made["venue"]["id"])
    assert client.post(f"/admin/events/{made['id']}/approve", headers=auth(admin_token)).status_code == 200
    row = _venue_row(db)
    assert (row.pending, row.hidden) == (False, True)
    assert "Tiny Stage" not in _public(client)


def test_a_hidden_pending_venue_stays_when_its_last_event_goes(client, user_token, admin_token, db):
    made = _submit(client, user_token)
    _hide(client, admin_token, made["venue"]["id"])
    assert client.delete(f"/events/{made['id']}", headers=auth(admin_token)).status_code == 204
    assert _venue_row(db).hidden is True


def test_a_merged_venue_keeps_the_kept_venues_state(client, db, admin_token):
    hidden, shown = venue(db, "Old Hall"), venue(db, "New Hall")
    _hide(client, admin_token, hidden.id)
    assert client.post(f"/venues/{hidden.id}/merge-into/{shown.id}", headers=auth(admin_token)).status_code == 200
    assert "New Hall" in _public(client)
