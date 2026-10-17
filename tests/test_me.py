from app.models import EventStatus

from .conftest import auth, register


def test_attended_round_trip(client, user_token, make_event):
    ev = make_event("Hamlet")
    h = auth(user_token)
    assert client.get("/me/attended").status_code == 401

    first = client.put(f"/me/attended/{ev.id}", headers=h)
    assert first.status_code == 201 and first.json()["event"]["title"] == "Hamlet"
    again = client.put(f"/me/attended/{ev.id}", headers=h)
    assert again.status_code == 200  # idempotent

    assert [a["event"]["id"] for a in client.get("/me/attended", headers=h).json()] == [ev.id]
    assert client.delete(f"/me/attended/{ev.id}", headers=h).status_code == 204
    assert client.get("/me/attended", headers=h).json() == []
    assert client.delete(f"/me/attended/{ev.id}", headers=h).status_code == 204  # also idempotent


def test_attended_lists_are_private(client, user_token, make_event):
    ev = make_event("Hamlet")
    client.put(f"/me/attended/{ev.id}", headers=auth(user_token))
    other = register(client, email="bo@example.com")
    assert client.get("/me/attended", headers=auth(other)).json() == []


def test_cannot_mark_missing_or_unapproved_events(client, user_token, make_event):
    pending = make_event("Pending", status=EventStatus.pending)
    assert client.put(f"/me/attended/{pending.id}", headers=auth(user_token)).status_code == 404
    assert client.put("/me/attended/99999", headers=auth(user_token)).status_code == 404


def test_deleting_an_event_removes_attended_marks(client, user_token, admin_token, make_event):
    ev = make_event("Hamlet")
    client.put(f"/me/attended/{ev.id}", headers=auth(user_token))
    assert client.delete(f"/events/{ev.id}", headers=auth(admin_token)).status_code == 204
    assert client.get("/me/attended", headers=auth(user_token)).json() == []


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
