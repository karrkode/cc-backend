"""CC-30: an opt-in "Suggested by <display name>" credit on events."""
from sqlalchemy import select

from app.models import User

from .conftest import auth, register

VENUE = {"name": "Annoyance", "neighborhood": "Lakeview"}


def submit(client, token, title, **extra):
    r = client.post("/events", headers=auth(token), json={"title": title, "category": "Comedy", "venue": VENUE, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def approve(client, admin_token, event_id):
    assert client.post(f"/admin/events/{event_id}/approve", headers=auth(admin_token)).status_code == 200


def public(client, event_id):
    return client.get(f"/events/{event_id}").json()


def test_credit_only_when_asked_for(client, user_token, admin_token):
    credited = submit(client, user_token, "Credit me", credit_me=True)
    quiet = submit(client, user_token, "Leave me out")  # the default is no credit
    for ev in (credited, quiet):
        approve(client, admin_token, ev["id"])

    assert public(client, credited["id"])["submitted_by_name"] == "Ana"
    assert public(client, quiet["id"])["submitted_by_name"] is None
    listed = {e["title"]: e["submitted_by_name"] for e in client.get("/events").json()["items"]}
    assert listed == {"Credit me": "Ana", "Leave me out": None}


def test_only_the_display_name_is_ever_public(client, user_token, admin_token):
    ev = submit(client, user_token, "Credit me", credit_me=True)
    approve(client, admin_token, ev["id"])
    for body in (client.get(f"/events/{ev['id']}").text, client.get("/events").text):
        assert "ana@example.com" not in body and '"submitted_by"' not in body


def test_submitter_cannot_be_named_in_the_request(client, user_token, admin_token):
    ev = submit(client, user_token, "Sneaky", credit_me=True, submitted_by_name="Someone Famous",
                submitted_by="00000000-0000-0000-0000-000000000000")
    approve(client, admin_token, ev["id"])
    assert public(client, ev["id"])["submitted_by_name"] == "Ana"  # always the signed-in user


def test_no_display_name_means_no_credit(client, admin_token):
    nameless = register(client, email="noname@example.com", name="")
    ev = submit(client, nameless, "Who sent this", credit_me=True)
    approve(client, admin_token, ev["id"])
    assert public(client, ev["id"])["submitted_by_name"] is None


def test_credit_disappears_with_the_account(client, user_token, admin_token, db):
    ev = submit(client, user_token, "Orphan", credit_me=True)
    approve(client, admin_token, ev["id"])
    db.delete(db.scalar(select(User).where(User.email == "ana@example.com")))
    db.commit()
    assert public(client, ev["id"])["submitted_by_name"] is None


def test_sheet_events_have_no_credit(client, make_event):
    ev = make_event("From the sheet")
    assert public(client, ev.id)["submitted_by_name"] is None


def test_submitter_and_admins_see_the_choice(client, user_token, admin_token):
    ev = submit(client, user_token, "Credit me", credit_me=True)
    assert ev["submitted_by_name"] == "Ana"  # the submitter sees how it will read
    [mine] = client.get("/me/submissions", headers=auth(user_token)).json()
    assert mine["submitted_by_name"] == "Ana"
    [queued] = client.get("/admin/events", headers=auth(admin_token)).json()["items"]
    assert queued["credit_me"] is True and queued["submitted_by_name"] == "Ana"
