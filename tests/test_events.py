from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models import Area, Event, EventStatus, User, Venue

from .conftest import auth

TODAY = date.today()


def titles(resp):
    assert resp.status_code == 200, resp.text
    return [e["title"] for e in resp.json()["items"]]


def test_public_list_shows_only_approved_current_events(client, make_event):
    make_event("Hamlet")
    make_event("Pending show", status=EventStatus.pending)
    make_event("Rejected show", status=EventStatus.rejected)
    make_event("Closed show", begin=TODAY - timedelta(days=60), end=TODAY - timedelta(days=1))

    assert titles(client.get("/events")) == ["Hamlet"]
    assert set(titles(client.get("/events?include_past=true"))) == {"Hamlet", "Closed show"}


def test_filters(client, make_event):
    make_event("Hamlet", min_price=Decimal("35"))
    make_event("Free gallery", venue_name="Gallery", neighborhood="Evanston, IL", area=Area.suburbs,
               category="Visual Art", min_price=Decimal("0"))
    make_event("No price", category="Comedy")
    make_event("Next month", begin=TODAY + timedelta(days=40), end=TODAY + timedelta(days=50))

    assert titles(client.get("/events?area=suburbs")) == ["Free gallery"]
    assert set(titles(client.get("/events?category=Theater&category=Comedy"))) == {"Hamlet", "No price", "Next month"}
    assert titles(client.get("/events?max_price=0")) == ["Free gallery"]
    assert set(titles(client.get("/events?max_price=40"))) == {"Hamlet", "Free gallery"}
    assert titles(client.get("/events?q=haml")) == ["Hamlet"]
    assert titles(client.get("/events?q=gallery")) == ["Free gallery"]  # matches venue name too
    assert titles(client.get("/events?q=%25")) == []  # % is searched for literally, not as a wildcard
    soon = (TODAY + timedelta(days=7)).isoformat()
    assert "Next month" not in titles(client.get(f"/events?to={soon}"))
    later = (TODAY + timedelta(days=45)).isoformat()
    assert titles(client.get(f"/events?from={later}")) == ["Next month"]
    assert client.get("/events?area=mars").status_code == 422


def test_order_and_pagination(client, make_event):
    make_event("B", begin=TODAY - timedelta(days=1))
    make_event("A", begin=TODAY - timedelta(days=9))
    make_event("TBD", begin=None)
    page = client.get("/events?limit=2").json()
    assert page["total"] == 3 and page["limit"] == 2 and len(page["items"]) == 2
    rest = client.get("/events?limit=2&offset=2").json()
    assert len(rest["items"]) == 1
    all_titles = [e["title"] for e in page["items"] + rest["items"]]
    assert all_titles == ["TBD", "A", "B"]  # no start date first, then soonest


def test_event_detail_visibility(client, make_event, user_token, admin_token):
    approved = make_event("Hamlet")
    pending = make_event("Pending show", status=EventStatus.pending)
    assert client.get(f"/events/{approved.id}").status_code == 200
    assert client.get(f"/events/{pending.id}").status_code == 404
    assert client.get(f"/events/{pending.id}", headers=auth(user_token)).status_code == 404  # not theirs
    assert client.get(f"/events/{pending.id}", headers=auth(admin_token)).status_code == 200
    assert client.get("/events/99999").status_code == 404


def test_user_submission_waits_for_approval(client, user_token, db):
    body = {"title": "New Play", "category": "Theater", "begin_date": "2030-01-10", "end_date": "2030-02-10",
            "venue": {"name": "Tiny Stage", "neighborhood": "Oak Park, IL"}, "url": "https://tiny.example/play"}
    r = client.post("/events", json=body, headers=auth(user_token))
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["status"] == "pending"
    assert ev["venue"]["area"] == "suburbs"  # worked out from "Oak Park, IL"

    assert "New Play" not in titles(client.get("/events?include_past=true"))
    assert client.get(f"/events/{ev['id']}", headers=auth(user_token)).status_code == 200  # submitter sees it
    mine = client.get("/me/submissions", headers=auth(user_token)).json()
    assert [e["title"] for e in mine] == ["New Play"]

    # submitting again with the same venue (different case) reuses it
    body["title"] = "Second Play"
    body["venue"]["name"] = "TINY STAGE"
    client.post("/events", json=body, headers=auth(user_token))
    assert len(db.scalars(select(Venue).where(Venue.name.ilike("tiny stage"))).all()) == 1


def test_admin_publish_now(client, admin_token, make_event):
    venue_id = make_event("Seed").venue_id
    r = client.post("/events", headers=auth(admin_token), json={
        "title": "Admin Show", "category": "Music", "venue_id": venue_id, "min_price": "20.00", "publish": "now"})
    assert r.status_code == 201 and r.json()["status"] == "approved" and r.json()["publish_at"] is None
    assert "Admin Show" in titles(client.get("/events"))


def test_admin_review_later_is_the_default(client, admin_token, make_event):
    venue_id = make_event("Seed").venue_id
    body = {"title": "Needs a look", "category": "Theater", "venue_id": venue_id}
    assert client.post("/events", headers=auth(admin_token), json=body).json()["status"] == "pending"
    body = {**body, "title": "Explicitly later", "publish": "review"}
    assert client.post("/events", headers=auth(admin_token), json=body).json()["status"] == "pending"
    assert client.get("/admin/events", headers=auth(admin_token)).json()["total"] == 2


def test_admin_publish_later(client, admin_token, user_token, make_event, db):
    venue_id = make_event("Seed").venue_id
    when = datetime.now(UTC) + timedelta(days=3)
    r = client.post("/events", headers=auth(admin_token), json={
        "title": "Opening Soon", "category": "Theater", "venue_id": venue_id,
        "publish": "later", "publish_at": when.isoformat()})
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["status"] == "approved" and ev["publish_at"] is not None

    # hidden from the public, from other users, and from "I've been" until then
    assert "Opening Soon" not in titles(client.get("/events?include_past=true"))
    assert client.get(f"/events/{ev['id']}").status_code == 404
    assert client.get(f"/events/{ev['id']}", headers=auth(user_token)).status_code == 404
    assert client.put(f"/me/attended/{ev['id']}", headers=auth(user_token)).status_code == 404
    assert client.get(f"/events/{ev['id']}", headers=auth(admin_token)).status_code == 200
    assert [e["title"] for e in client.get("/admin/events/scheduled", headers=auth(admin_token)).json()] == ["Opening Soon"]

    # the time arrives: it's live, with nothing having to run
    event = db.get(Event, ev["id"])
    event.publish_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    assert "Opening Soon" in titles(client.get("/events"))
    assert client.get("/admin/events/scheduled", headers=auth(admin_token)).json() == []


def test_publish_now_brings_a_scheduled_event_forward(client, admin_token, make_event):
    venue_id = make_event("Seed").venue_id
    ev = client.post("/events", headers=auth(admin_token), json={
        "title": "Later", "category": "Theater", "venue_id": venue_id, "publish": "later",
        "publish_at": (datetime.now(UTC) + timedelta(days=9)).isoformat()}).json()
    r = client.post(f"/admin/events/{ev['id']}/approve", headers=auth(admin_token))
    assert r.json()["publish_at"] is None
    assert "Later" in titles(client.get("/events"))


def test_publish_options_are_admin_only(client, user_token, make_event):
    venue_id = make_event("Seed").venue_id
    for extra in ({"publish": "now"},
                  {"publish": "later", "publish_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}):
        r = client.post("/events", headers=auth(user_token), json={
            "title": "Sneaky", "category": "Theater", "venue_id": venue_id, **extra})
        assert r.status_code == 201 and r.json()["status"] == "pending" and r.json()["publish_at"] is None


def test_publish_later_validation(client, admin_token, make_event):
    venue_id = make_event("Seed").venue_id
    base = {"title": "X", "category": "Theater", "venue_id": venue_id}
    h = auth(admin_token)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert client.post("/events", json={**base, "publish": "later"}, headers=h).status_code == 422
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert client.post("/events", json={**base, "publish": "later", "publish_at": past}, headers=h).status_code == 422
    naive = (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()
    assert client.post("/events", json={**base, "publish": "later", "publish_at": naive}, headers=h).status_code == 422
    assert client.post("/events", json={**base, "publish": "now", "publish_at": future}, headers=h).status_code == 422
    assert client.post("/events", json={**base, "publish": "soon"}, headers=h).status_code == 422


def test_create_event_validation(client, user_token, make_event):
    venue_id = make_event().venue_id
    base = {"title": "X", "category": "Theater", "venue_id": venue_id}
    h = auth(user_token)
    assert client.post("/events", json=base).status_code == 401
    assert client.post("/events", json={**base, "category": "Rodeo"}, headers=h).status_code == 422
    assert client.post("/events", json={**base, "begin_date": "2030-02-01", "end_date": "2030-01-01"},
                       headers=h).status_code == 422
    assert client.post("/events", json={**base, "venue_id": 99999}, headers=h).status_code == 422
    assert client.post("/events", json={**base, "venue": {"name": "Both"}}, headers=h).status_code == 422
    assert client.post("/events", json={"title": "X", "category": "Theater"}, headers=h).status_code == 422
    assert client.post("/events", json={**base, "url": "javascript:alert(1)"}, headers=h).status_code == 422
    assert client.post("/events", json={**base, "min_price": "-5"}, headers=h).status_code == 422


def test_only_admins_edit_and_delete(client, user_token, admin_token, make_event):
    ev = make_event("Hamlet")
    assert client.patch(f"/events/{ev.id}", json={"title": "Hacked"}, headers=auth(user_token)).status_code == 403
    assert client.delete(f"/events/{ev.id}", headers=auth(user_token)).status_code == 403

    r = client.patch(f"/events/{ev.id}", json={"title": "Hamlet (extended)", "end_date": "2099-01-01"},
                     headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["title"] == "Hamlet (extended)"
    bad = client.patch(f"/events/{ev.id}", json={"end_date": "2000-01-01"}, headers=auth(admin_token))
    assert bad.status_code == 422

    assert client.delete(f"/events/{ev.id}", headers=auth(admin_token)).status_code == 204
    assert client.get(f"/events/{ev.id}").status_code == 404
    assert client.delete(f"/events/{ev.id}", headers=auth(admin_token)).status_code == 404


def test_review_queue(client, user_token, admin_token, make_event):
    make_event("Live already")
    p1 = make_event("First submission", status=EventStatus.pending)
    p2 = make_event("Second submission", status=EventStatus.pending)

    assert client.get("/admin/events", headers=auth(user_token)).status_code == 403
    queue = client.get("/admin/events", headers=auth(admin_token)).json()
    assert [e["title"] for e in queue["items"]] == ["First submission", "Second submission"]

    assert client.post(f"/admin/events/{p1.id}/approve", headers=auth(admin_token)).json()["status"] == "approved"
    assert client.post(f"/admin/events/{p2.id}/reject", headers=auth(admin_token)).json()["status"] == "rejected"
    assert "First submission" in titles(client.get("/events"))
    assert "Second submission" not in titles(client.get("/events"))
    assert client.get("/admin/events", headers=auth(admin_token)).json()["total"] == 0
    assert client.get("/admin/events?status=rejected", headers=auth(admin_token)).json()["total"] == 1
    assert client.post("/admin/events/99999/approve", headers=auth(admin_token)).status_code == 404


def test_admins_see_who_submitted(client, user_token, admin_token, make_event):
    sent = client.post("/events", headers=auth(user_token), json={
        "title": "Fresh pick", "category": "Comedy", "venue": {"name": "Annoyance", "neighborhood": "Lakeview"}}).json()
    make_event("From the sheet", status=EventStatus.pending)  # no submitter

    items = {e["title"]: e for e in client.get("/admin/events", headers=auth(admin_token)).json()["items"]}
    who = items["Fresh pick"]["submitted_by"]
    assert who["display_name"] == "Ana" and who["email"] == "ana@example.com" and who["id"]
    assert items["From the sheet"]["submitted_by"] is None

    approved = client.post(f"/admin/events/{sent['id']}/approve", headers=auth(admin_token)).json()
    assert approved["submitted_by"]["email"] == "ana@example.com"


def test_scheduled_list_shows_the_submitter(client, admin_token, make_event):
    later = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    client.post("/events", headers=auth(admin_token), json={
        "title": "Soon", "category": "Theater", "venue": {"name": "Goodman Theatre", "neighborhood": "Loop"},
        "publish": "later", "publish_at": later})
    [ev] = client.get("/admin/events/scheduled", headers=auth(admin_token)).json()
    assert ev["submitted_by"]["display_name"] == "Admin"


def test_submitter_is_null_once_their_account_is_gone(client, user_token, admin_token, db):
    client.post("/events", headers=auth(user_token), json={
        "title": "Orphan", "category": "Comedy", "venue": {"name": "Annoyance", "neighborhood": "Lakeview"}})
    db.delete(db.scalar(select(User).where(User.email == "ana@example.com")))
    db.commit()
    [ev] = client.get("/admin/events", headers=auth(admin_token)).json()["items"]
    assert ev["title"] == "Orphan" and ev["submitted_by"] is None


def test_submitter_details_never_leave_the_admin_routes(client, user_token, admin_token, make_event):
    sent = client.post("/events", headers=auth(user_token), json={
        "title": "Mine", "category": "Comedy", "venue": {"name": "Annoyance", "neighborhood": "Lakeview"}})
    assert "submitted_by" not in sent.json()
    ev_id = sent.json()["id"]
    assert client.get("/admin/events", headers=auth(user_token)).status_code == 403
    assert client.get("/admin/events/scheduled", headers=auth(user_token)).status_code == 403
    client.post(f"/admin/events/{ev_id}/approve", headers=auth(admin_token))

    for token in (None, user_token, admin_token):  # public routes stay public-shaped, even for admins
        h = auth(token) if token else {}
        assert "submitted_by" not in client.get(f"/events/{ev_id}", headers=h).json()
        assert all("submitted_by" not in e for e in client.get("/events", headers=h).json()["items"])
    patched = client.patch(f"/events/{ev_id}", json={"title": "Mine!"}, headers=auth(admin_token)).json()
    assert "submitted_by" not in patched


def test_venues(client, admin_token, user_token):
    body = {"name": "Steppenwolf Theatre", "neighborhood": "Lincoln Park"}
    assert client.post("/venues", json=body, headers=auth(user_token)).status_code == 403
    r = client.post("/venues", json=body, headers=auth(admin_token))
    assert r.status_code == 201 and r.json()["area"] == "city"
    assert client.post("/venues", json=body, headers=auth(admin_token)).status_code == 409
    assert [v["name"] for v in client.get("/venues?q=steppen").json()] == ["Steppenwolf Theatre"]
