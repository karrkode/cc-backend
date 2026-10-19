"""CC-56: what the site's admin edit form does with PATCH and DELETE /events/{id}."""
from datetime import date, timedelta

from sqlalchemy import func, select

from app.models import Attended, Plan, Venue

from .conftest import auth

T = date.today()


def test_the_edit_form_can_change_every_field(client, admin_token, make_event, db):
    ev = make_event("Hamlet")
    steppenwolf = Venue(name="Steppenwolf", neighborhood="Lincoln Park")
    db.add(steppenwolf); db.commit()
    form = {"title": "Hamlet (extended)", "venue_id": steppenwolf.id, "begin_date": None, "end_date": None,
            "open_run": True, "cost_text": "$25–$60", "min_price": "25", "category": "Comedy",
            "url": "https://example.org/hamlet2", "status": "approved", "publish_at": None}
    r = client.patch(f"/events/{ev.id}", json=form, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["title"], got["venue"]["name"], got["begin_date"], got["end_date"], got["open_run"], got["cost_text"],
            got["min_price"], got["category"], got["url"]) == \
        ("Hamlet (extended)", "Steppenwolf", None, None, True, "$25–$60", 25.0, "Comedy", "https://example.org/hamlet2")
    r = client.patch(f"/events/{ev.id}", json={"min_price": None}, headers=auth(admin_token))
    assert r.json()["min_price"] is None  # price can be cleared ("not posted")


def test_bad_edits_are_refused(client, admin_token, make_event):
    ev = make_event("Hamlet")
    h = auth(admin_token)
    for bad in ({"url": "javascript:alert(1)"}, {"title": None}, {"title": ""}, {"category": "Opera"},
                {"venue_id": 99999}, {"status": None}, {"min_price": "-1"},
                {"begin_date": (T + timedelta(days=9)).isoformat(), "end_date": (T + timedelta(days=1)).isoformat()},
                {"publish_at": "2030-01-01T10:00:00"}):  # no time zone
        assert client.patch(f"/events/{ev.id}", json=bad, headers=h).status_code == 422, bad
    assert client.get(f"/events/{ev.id}").json()["title"] == "Hamlet"


def test_withdraw_hides_it_and_restore_brings_it_back(client, admin_token, make_event):
    ev = make_event("Hamlet")
    h = auth(admin_token)
    assert client.patch(f"/events/{ev.id}", json={"status": "withdrawn"}, headers=h).status_code == 200
    assert client.get(f"/events/{ev.id}").status_code == 404
    assert "Hamlet" not in [e["title"] for e in client.get("/events").json()["items"]]
    listed = client.get("/admin/events", params={"status": "withdrawn"}, headers=h).json()["items"]
    assert [e["title"] for e in listed] == ["Hamlet"]  # where the site lists withdrawn events to restore
    assert client.patch(f"/events/{ev.id}", json={"status": "approved"}, headers=h).status_code == 200
    assert client.get(f"/events/{ev.id}").status_code == 200


def test_delete_also_removes_marks_and_plans(client, admin_token, user_token, make_event, db):
    ev, keep = make_event("Hamlet"), make_event("Bug")
    for e in (ev, keep):
        client.put(f"/me/attended/{e.id}", headers=auth(user_token))
        client.put(f"/me/plans/{e.id}", json={"status": "interested"}, headers=auth(user_token))
    assert client.delete(f"/events/{ev.id}", headers=auth(admin_token)).status_code == 204
    assert db.scalar(select(func.count()).select_from(Attended)) == 1
    assert db.scalar(select(func.count()).select_from(Plan)) == 1
    assert [p["event"]["title"] for p in client.get("/me/plans", headers=auth(user_token)).json()] == ["Bug"]
