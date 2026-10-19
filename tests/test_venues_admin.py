"""CC-57: admins rename, merge and delete venues."""
from sqlalchemy import select

from app.models import Event, Venue

from .conftest import auth


def venue(db, name, hood="Loop"):
    return db.scalar(select(Venue).where(Venue.name == name, Venue.neighborhood == hood))


def manage(client, token, **params):
    r = client.get("/venues/manage", params=params, headers=auth(token))
    assert r.status_code == 200, r.text
    return {(v["name"], v["neighborhood"]): (v["events"], v["sheet_events"]) for v in r.json()}


def test_list_counts_every_event_and_the_sheet_ones(client, admin_token, make_event, db):
    from app.models import EventStatus
    make_event("Hamlet"); make_event("Pending one", status=EventStatus.pending)
    sheet = make_event("From the sheet", venue_name="Steppenwolf", neighborhood="Lincoln Park")
    sheet.source_id = "abc123"; db.add(Venue(name="Empty Hall", neighborhood="Pilsen")); db.commit()
    assert manage(client, admin_token) == {("Empty Hall", "Pilsen"): (0, 0), ("Goodman Theatre", "Loop"): (2, 0),
                                          ("Steppenwolf", "Lincoln Park"): (1, 1)}
    assert list(manage(client, admin_token, q="lincoln")) == [("Steppenwolf", "Lincoln Park")]


def test_rename_and_move(client, admin_token, make_event, db):
    make_event("Hamlet")
    v = venue(db, "Goodman Theatre")
    r = client.patch(f"/venues/{v.id}", json={"name": " The Goodman ", "neighborhood": "Evanston, IL"}, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["neighborhood"], r.json()["area"], r.json()["events"]) == \
        ("The Goodman", "Evanston, IL", "suburbs", 1)  # area worked out from the new neighborhood
    r = client.patch(f"/venues/{v.id}", json={"area": "out_of_town"}, headers=auth(admin_token))
    assert r.json()["area"] == "out_of_town" and r.json()["name"] == "The Goodman"
    assert client.get("/events").json()["items"][0]["venue"]["name"] == "The Goodman"


def test_rename_cant_clash_with_another_venue(client, admin_token, db):
    db.add_all([Venue(name="Steppenwolf", neighborhood="Lincoln Park"), Venue(name="Steppenwolf Theatre", neighborhood="Lincoln Park")])
    db.commit()
    v = venue(db, "Steppenwolf Theatre", "Lincoln Park")
    r = client.patch(f"/venues/{v.id}", json={"name": "STEPPENWOLF"}, headers=auth(admin_token))
    assert r.status_code == 409 and "Merge" in r.json()["detail"]
    assert client.patch(f"/venues/{v.id}", json={"name": ""}, headers=auth(admin_token)).status_code == 422
    assert client.patch(f"/venues/{v.id}", json={"name": None}, headers=auth(admin_token)).status_code == 422
    # same venue, different capitals: not a clash with itself
    assert client.patch(f"/venues/{v.id}", json={"name": "STEPPENWOLF THEATRE"}, headers=auth(admin_token)).status_code == 200


def test_merge_moves_the_events_and_removes_the_duplicate(client, admin_token, make_event, db):
    make_event("Bug", venue_name="Steppenwolf Theater", neighborhood="Lincoln Park")
    make_event("Purpose", venue_name="Steppenwolf Theater", neighborhood="Lincoln Park")
    make_event("Hamlet", venue_name="Steppenwolf", neighborhood="Lincoln Park")
    dupe = venue(db, "Steppenwolf Theater", "Lincoln Park").id
    keep = venue(db, "Steppenwolf", "Lincoln Park").id
    r = client.post(f"/venues/{dupe}/merge-into/{keep}", headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["moved"] == 2 and r.json()["into"]["name"] == "Steppenwolf"
    db.expire_all()
    assert db.get(Venue, dupe) is None
    assert {e.venue_id for e in db.scalars(select(Event))} == {keep}
    assert client.post(f"/venues/{keep}/merge-into/{keep}", headers=auth(admin_token)).status_code == 422
    assert client.post(f"/venues/{keep}/merge-into/99999", headers=auth(admin_token)).status_code == 404


def test_delete_only_when_unused(client, admin_token, make_event, db):
    from app.models import EventStatus
    make_event("Withdrawn one", status=EventStatus.withdrawn)  # still counts
    used = venue(db, "Goodman Theatre")
    db.add(Venue(name="Empty Hall", neighborhood="Pilsen")); db.commit()
    empty = venue(db, "Empty Hall", "Pilsen")
    r = client.delete(f"/venues/{used.id}", headers=auth(admin_token))
    assert r.status_code == 409 and r.json()["detail"].startswith("1 event still use")
    assert client.delete(f"/venues/{empty.id}", headers=auth(admin_token)).status_code == 204
    assert client.delete(f"/venues/{empty.id}", headers=auth(admin_token)).status_code == 404


def test_admins_only(client, user_token, make_event, db):
    make_event("Hamlet")
    v = venue(db, "Goodman Theatre")
    h = auth(user_token)
    assert client.get("/venues/manage", headers=h).status_code == 403
    assert client.patch(f"/venues/{v.id}", json={"name": "Mine"}, headers=h).status_code == 403
    assert client.post(f"/venues/{v.id}/merge-into/{v.id}", headers=h).status_code == 403
    assert client.delete(f"/venues/{v.id}", headers=h).status_code == 403
    assert client.get("/venues/manage").status_code == 401


def test_sheet_sync_puts_sheet_events_back_on_the_sheets_venue(client, admin_token, db):
    """The behaviour the site's note describes: a renamed sheet venue is re-created by the sync."""
    from app.sync import apply_rows
    row = dict(id="abc123", title="Bug", venue="Steppenwolf", hood="Lincoln Park", area="City", begin="2026-01-01",
               end="2099-01-01", openRun=False, cost="", min=None, type="Theater", url="")
    apply_rows(db, [row], withdraw_missing=False); db.commit()
    v = venue(db, "Steppenwolf", "Lincoln Park")
    assert client.patch(f"/venues/{v.id}", json={"name": "Steppenwolf Theatre"}, headers=auth(admin_token)).status_code == 200
    db.expire_all()  # the rename happened in the API's session
    apply_rows(db, [row], withdraw_missing=False); db.commit()
    db.expire_all()
    ev = db.scalar(select(Event).where(Event.source_id == "abc123"))
    assert db.get(Venue, ev.venue_id).name == "Steppenwolf"
