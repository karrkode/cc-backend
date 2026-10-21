"""CC-98: the Cultural Journal: three words, a review and "Would you recommend?" on an "I've been" mark,
private to its owner, and gone with the mark."""
from datetime import timedelta

from .conftest import auth, register
from .test_crew_plans import plan, setup, share


def been(client, h, ev):
    assert client.put(f"/me/attended/{ev.id}", headers=h).status_code in (200, 201)


def journal(client, h, ev, **body):
    return client.put(f"/me/attended/{ev.id}/journal", json=body, headers=h)


def test_save_and_reload_an_entry(client, make_event, user_token):
    h, hamlet = auth(user_token), make_event("Hamlet")
    assert journal(client, h, hamlet, words="Moody").status_code == 404  # not been yet
    been(client, h, hamlet)
    row = client.get("/me/attended", headers=h).json()[0]
    assert (row["words"], row["review"], row["recommend"]) == ("", "", False)  # an empty entry is fine
    r = journal(client, h, hamlet, words="  dark   and stormy ", review=" Loved the ghost. ", recommend=True)
    assert r.status_code == 200 and r.json() == {"words": "dark and stormy", "review": "Loved the ghost.", "recommend": True}
    row = client.get("/me/attended", headers=h).json()[0]
    assert (row["words"], row["review"], row["recommend"]) == ("dark and stormy", "Loved the ghost.", True)
    assert journal(client, h, hamlet).json() == {"words": "", "review": "", "recommend": False}  # cleared


def test_limits(client, make_event, user_token):
    h, hamlet = auth(user_token), make_event("Hamlet")
    been(client, h, hamlet)
    r = journal(client, h, hamlet, words="one two three four")
    assert r.status_code == 422 and "three words" in r.text
    assert journal(client, h, hamlet, review="x" * 1001).status_code == 422
    assert journal(client, h, hamlet, review="x" * 1000).status_code == 200


def test_newest_first_and_gone_with_the_mark(client, make_event, user_token, db):
    h = auth(user_token)
    events = [make_event(f"Show {i}") for i in range(6)]
    for ev in events:
        been(client, h, ev)
    from app.models import Attended
    for i, mark in enumerate(db.query(Attended).order_by(Attended.event_id)):  # spread the marks out in time
        mark.created_at = mark.created_at - timedelta(days=10 - i)
    db.commit()
    assert [r["event"]["title"] for r in client.get("/me/attended", headers=h).json()][:5] == [f"Show {i}" for i in (5, 4, 3, 2, 1)]
    journal(client, h, events[5], words="gone soon")
    assert client.delete(f"/me/attended/{events[5].id}", headers=h).status_code == 204
    been(client, h, events[5])
    row = next(r for r in client.get("/me/attended", headers=h).json() if r["event"]["id"] == events[5].id)
    assert row["words"] == ""  # un-marking deleted it; marking again starts fresh


def test_nobody_else_sees_it(client, make_event, admin_token):
    p, crew, _, hamlet, _ = setup(client, make_event)
    plan(client, p["Ben"], hamlet)
    share(client, p["Ben"], hamlet, [crew["id"]])
    been(client, p["Ben"], hamlet)  # Ana's crew sees Ben's been (CC-93), but never his journal
    assert journal(client, p["Ben"], hamlet, words="Secretword", review="Privatereview", recommend=True).status_code == 200
    assert journal(client, p["Ana"], hamlet, words="mine").status_code == 404  # Ana can't write to Ben's mark
    admin = auth(admin_token)
    seen = [client.get("/crews", headers=p["Ana"]), client.get(f"/events/{hamlet.id}", headers=p["Ana"]),
            client.get("/me/attended", headers=p["Ana"]), client.get(f"/events/{hamlet.id}", headers=admin),
            client.get("/events?include_past=true", headers=admin), client.get("/admin/events?status=approved", headers=admin),
            client.get("/admin/stats", headers=admin), client.get("/admin/stats/events", headers=admin), client.get(f"/events/{hamlet.id}")]
    for r in seen:
        assert r.status_code == 200 and "Secretword" not in r.text and "Privatereview" not in r.text, r.request.url
    assert "Secretword" in client.get("/me/attended", headers=p["Ben"]).text
