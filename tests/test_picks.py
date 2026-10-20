"""CC-66: the owner's picks: plans by accounts with the dolphins setting, public on every event."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import User

from .conftest import auth, register

T = date.today()


def owner(client, db, email="owner@example.com", name="Andrew"):
    token = register(client, email=email, name=name)
    db.scalar(select(User).where(User.email == email)).dolphins = True
    db.commit()
    return token


def plan(client, token, ev, status, day=None):
    body = {"status": status, **({"date": day.isoformat()} if day else {})}
    assert client.put(f"/me/plans/{ev.id}", json=body, headers=auth(token)).status_code in (200, 201)


def picks_by_title(client, **params):
    r = client.get("/events", params={"sort": "az", **params})
    assert r.status_code == 200, r.text
    return {e["title"]: e["pick"] for e in r.json()["items"]}


def test_owner_plans_show_to_everyone_and_members_plans_dont(client, db, user_token, make_event):
    a, b, c = make_event("Alpha"), make_event("Bravo"), make_event("Charlie")
    me = owner(client, db)
    plan(client, me, a, "interested")
    plan(client, me, b, "going", T + timedelta(days=3))
    plan(client, user_token, c, "going", T + timedelta(days=1))  # a regular member: private
    got = picks_by_title(client)  # signed out
    assert got["Alpha"] == {"status": "interested", "date": None, "by": "Andrew"}
    assert got["Bravo"] == {"status": "going", "date": (T + timedelta(days=3)).isoformat(), "by": "Andrew"}
    assert got["Charlie"] is None
    assert client.get(f"/events/{b.id}").json()["pick"]["status"] == "going"
    assert client.get("/events/lookup", params={"ids": str(a.id)}).json()[0]["pick"]["status"] == "interested"
    # the member's own lists carry the picks too
    assert client.get("/me/plans", headers=auth(user_token)).json()[0]["event"]["pick"] is None


def test_going_beats_interested_across_owner_accounts(client, db, make_event):
    ev = make_event("Hamlet")
    one, two = owner(client, db), owner(client, db, email="info@example.com", name="Culture Crew")
    plan(client, one, ev, "interested")
    plan(client, two, ev, "going", T + timedelta(days=5))
    assert picks_by_title(client)["Hamlet"] == {"status": "going", "date": (T + timedelta(days=5)).isoformat(), "by": "Culture Crew"}
    client.delete(f"/me/plans/{ev.id}", headers=auth(two))
    assert picks_by_title(client)["Hamlet"]["status"] == "interested"


def test_picks_filter_for_everyone(client, db, user_token, make_event):
    a, b, c, d = (make_event(t) for t in ("Alpha", "Bravo", "Charlie", "Delta"))
    me, other = owner(client, db), owner(client, db, email="info@example.com", name="Crew")
    plan(client, me, a, "interested")
    plan(client, me, b, "going", T)
    plan(client, me, c, "interested")
    plan(client, other, c, "going", T)  # going wins: Charlie is a going pick, not an interested one
    plan(client, user_token, d, "interested")  # members' plans are never picks
    assert list(picks_by_title(client, picks="interested")) == ["Alpha"]
    assert list(picks_by_title(client, picks="going")) == ["Bravo", "Charlie"]
    assert list(picks_by_title(client, picks=["interested", "going"])) == ["Alpha", "Bravo", "Charlie"]
    assert client.get("/events", params={"picks": "interested"}).json()["total"] == 1  # no sign-in needed
    assert client.get("/events", params={"picks": "nope"}).status_code == 422


def test_turning_dolphins_off_hides_the_picks(client, db, make_event):
    ev = make_event("Hamlet")
    me = owner(client, db)
    plan(client, me, ev, "interested")
    db.scalar(select(User).where(User.email == "owner@example.com")).dolphins = False
    db.commit()
    assert picks_by_title(client)["Hamlet"] is None
