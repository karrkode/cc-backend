"""CC-93: the crews you'd shared a plan with see that you've been, with whether your photo is approved."""
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.models import AttendedShare, Event, WentPhoto
from app.stats import today_in_chicago

from .conftest import auth, register
from .test_crew_plans import plan, setup, share


def board(client, h, crew_id):
    crew = next(c for c in client.get("/crews", headers=h).json() if c["id"] == crew_id)
    return {row["event"]["title"]: row for row in crew["plans"]}


def been(client, h, ev):
    assert client.put(f"/me/attended/{ev.id}", headers=h).status_code in (200, 201)


def test_crews_that_saw_the_plan_see_youve_been(client, make_event, db):
    p, crew, other, hamlet, sue = setup(client, make_event)
    plan(client, p["Ben"], hamlet)
    share(client, p["Ben"], hamlet, [crew["id"]])
    been(client, p["Ben"], hamlet)
    row = board(client, p["Ana"], crew["id"])["Hamlet"]
    assert row["people"] == [] and row["been"] == [{"name": "Ben", "approved": False}]  # the plan's done; the mark shows
    assert board(client, p["Ben"], crew["id"]) == {}  # never yourself

    # a plan never shared: nobody sees the mark
    plan(client, p["Ben"], sue)
    been(client, p["Ben"], sue)
    assert "Sue the T. rex" not in board(client, p["Ana"], crew["id"])

    # an approved photo makes it 📸
    db.add(WentPhoto(user_id=db.scalar(select(AttendedShare.user_id)), event_id=hamlet.id, key="photos/x.jpg",
                     content_type="image/jpeg", size=1, status="approved", labels=[]))
    db.commit()
    assert board(client, p["Ana"], crew["id"])["Hamlet"]["been"] == [{"name": "Ben", "approved": True}]

    # removing the mark, or leaving the crew, takes it away
    client.delete(f"/me/attended/{hamlet.id}", headers=p["Ben"])
    assert "Hamlet" not in board(client, p["Ana"], crew["id"])
    plan(client, p["Ben"], hamlet)
    share(client, p["Ben"], hamlet, [crew["id"]])
    been(client, p["Ben"], hamlet)
    assert client.post(f"/crews/{crew['id']}/leave", headers=p["Ben"]).status_code == 204
    assert db.scalar(select(AttendedShare)) is None


def test_approved_first_and_closed_events_stay_two_weeks(client, make_event, db):
    p, crew, _, hamlet, _ = setup(client, make_event)
    dee = auth(register(client, email="dee@example.com", name="Dee Park"))
    token = client.post(f"/crews/{crew['id']}/invites", headers=p["Ana"]).json()["token"]
    client.post("/crews/join", json={"token": token}, headers=dee)
    for h in (p["Ben"], dee):
        plan(client, h, hamlet)
        share(client, h, hamlet, [crew["id"]])
        been(client, h, hamlet)
    dee_id = uuid.UUID(client.get("/auth/me", headers=dee).json()["id"])
    db.add(WentPhoto(user_id=dee_id, event_id=hamlet.id, key="photos/d.jpg", content_type="image/jpeg", size=1, status="approved", labels=[]))
    db.commit()
    assert board(client, p["Ana"], crew["id"])["Hamlet"]["been"] == [{"name": "Dee Park", "approved": True},
                                                                     {"name": "Ben", "approved": False}]

    later = make_event("Macbeth")  # still on, so it comes before a closed one
    plan(client, p["Ben"], later)
    share(client, p["Ben"], later, [crew["id"]])
    event, T = db.get(Event, hamlet.id), today_in_chicago()  # the board's "today" is Chicago's, not the machine's
    event.begin_date, event.end_date = T - timedelta(days=40), T - timedelta(days=10)  # closed 10 days ago: still there
    db.commit()
    assert list(board(client, p["Ana"], crew["id"])) == ["Macbeth", "Hamlet"]
    event.end_date = T - timedelta(days=15)  # past the two weeks: gone
    db.commit()
    assert list(board(client, p["Ana"], crew["id"])) == ["Macbeth"]


def test_your_crews_catalog_counts_been_marks(client, make_event):
    """CC-94: Your Crews Catalog lists events a crew-mate is interested in, committed to, or has been to."""
    p, crew, _, hamlet, sue = setup(client, make_event)
    globe = make_event("The Globe")
    titles = lambda h: sorted(e["title"] for e in client.get("/events?crews=all", headers=h).json()["items"])
    plan(client, p["Ben"], hamlet, status="going", date=(today_in_chicago() + timedelta(days=2)).isoformat())
    share(client, p["Ben"], hamlet, [crew["id"]])
    plan(client, p["Ben"], sue)
    share(client, p["Ben"], sue, [crew["id"]])
    been(client, p["Ben"], sue)  # the plan ends; the mark keeps it in
    plan(client, p["Ben"], globe)
    been(client, p["Ben"], globe)  # never shared: doesn't count
    assert titles(p["Ana"]) == ["Hamlet", "Sue the T. rex"]
    assert titles(p["Ben"]) == []  # never your own
    assert titles(p["Cy"]) == []  # not in the crew
    assert client.post(f"/crews/{crew['id']}/leave", headers=p["Ben"]).status_code == 204
    assert titles(p["Ana"]) == []
