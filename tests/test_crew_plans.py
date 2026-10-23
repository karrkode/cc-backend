"""CC-75: sharing plans with crews: only the crews you choose, only their members, and gone when the plan is."""
from datetime import date, timedelta

from .conftest import auth, register

T = date.today()


def setup(client, make_event):
    """Ana's crew Thursday Theater with Ben in it; Cy is in no crew. Two events."""
    p = {n: auth(register(client, email=f"{n.lower()}@example.com", name=n)) for n in ("Ana", "Ben", "Cy")}
    crew = client.post("/crews", json={"name": "Thursday Theater"}, headers=p["Ana"]).json()
    other = client.post("/crews", json={"name": "Laughin Times"}, headers=p["Ana"]).json()
    token = client.post(f"/crews/{crew['id']}/invites", headers=p["Ana"]).json()["token"]
    client.post("/crews/join", json={"token": token}, headers=p["Ben"])
    hamlet, sue = make_event("Hamlet"), make_event("Sue the T. rex", category="Visual Art")
    return p, crew, other, hamlet, sue


def plan(client, h, ev, **body):
    r = client.put(f"/me/plans/{ev.id}", json=body or {"status": "interested"}, headers=h)
    assert r.status_code in (200, 201), r.text


def share(client, h, ev, crews):
    return client.put(f"/me/plans/{ev.id}/crews", json={"crews": crews}, headers=h)


def test_nothing_is_shared_until_you_choose(client, make_event):
    p, crew, _, hamlet, _ = setup(client, make_event)
    plan(client, p["Ben"], hamlet, status="going", date=(T + timedelta(days=2)).isoformat())
    assert client.get(f"/events/{hamlet.id}", headers=p["Ana"]).json()["crew_plans"] == []
    r = share(client, p["Ben"], hamlet, [crew["id"]])
    assert r.status_code == 200 and r.json()["crews"] == [crew["id"]]
    seen = client.get(f"/events/{hamlet.id}", headers=p["Ana"]).json()["crew_plans"]
    assert seen == [{"crew_id": crew["id"], "crew_name": "Thursday Theater", "going": ["Ben"], "interested": [], "been": []}]
    assert client.get(f"/events/{hamlet.id}", headers=p["Ben"]).json()["crew_plans"] == []  # never your own
    assert client.get(f"/events/{hamlet.id}", headers=p["Cy"]).json()["crew_plans"] == []  # not in the crew
    assert client.get(f"/events/{hamlet.id}").json()["crew_plans"] == []  # signed out
    assert [x["crews"] for x in client.get("/me/plans", headers=p["Ben"]).json()] == [[crew["id"]]]


def test_only_your_own_crews_and_your_own_plans(client, make_event):
    p, crew, other, hamlet, sue = setup(client, make_event)
    assert share(client, p["Ben"], hamlet, [crew["id"]]).status_code == 404  # no plan yet
    plan(client, p["Ben"], hamlet)
    assert share(client, p["Ben"], hamlet, [other["id"]]).status_code == 404  # Ben isn't in Laughin Times
    assert share(client, p["Cy"], hamlet, [crew["id"]]).status_code == 404


def test_the_crew_board_and_the_catalog_filter(client, make_event):
    p, crew, _, hamlet, sue = setup(client, make_event)
    plan(client, p["Ben"], hamlet, status="going", date=(T + timedelta(days=3)).isoformat()); share(client, p["Ben"], hamlet, [crew["id"]])
    plan(client, p["Ana"], hamlet); share(client, p["Ana"], hamlet, [crew["id"]])
    plan(client, p["Ana"], sue, status="going", date=(T + timedelta(days=1)).isoformat()); share(client, p["Ana"], sue, [crew["id"]])
    board = next(c for c in client.get("/crews", headers=p["Ben"]).json() if c["name"] == "Thursday Theater")["plans"]
    assert [b["event"]["title"] for b in board] == ["Sue the T. rex", "Hamlet"]  # soonest first
    assert [(x["name"], x["status"], x["you"]) for x in board[1]["people"]] == [("Ben", "going", True), ("Ana", "interested", False)]
    # the catalog filter: events other members shared; one crew or all of them
    titles = lambda h, q: sorted(e["title"] for e in client.get(f"/events?crews={q}", headers=h).json()["items"])
    assert titles(p["Ben"], "all") == ["Hamlet", "Sue the T. rex"]
    assert titles(p["Ana"], str(crew["id"])) == ["Hamlet"]  # Ben's; not Ana's own
    assert titles(p["Cy"], "all") == []
    assert client.get("/events?crews=all").status_code == 401


def test_sharing_ends_with_the_plan_or_the_membership(client, make_event):
    p, crew, _, hamlet, sue = setup(client, make_event)
    plan(client, p["Ben"], hamlet); share(client, p["Ben"], hamlet, [crew["id"]])
    plan(client, p["Ben"], hamlet, status="going", date=(T + timedelta(days=2)).isoformat())  # a changed plan stays shared
    assert client.get(f"/events/{hamlet.id}", headers=p["Ana"]).json()["crew_plans"][0]["going"] == ["Ben"]
    client.put(f"/me/attended/{hamlet.id}", headers=p["Ben"])  # "I've been" ends the plan, and the sharing
    # CC-117: ...and the crew sees that he's been instead
    assert client.get(f"/events/{hamlet.id}", headers=p["Ana"]).json()["crew_plans"] == [
        {"crew_id": crew["id"], "crew_name": "Thursday Theater", "going": [], "interested": [], "been": ["Ben"]}]
    plan(client, p["Ben"], hamlet)
    assert client.get("/me/plans", headers=p["Ben"]).json()[0]["crews"] == []  # a new plan starts unshared
    plan(client, p["Ben"], sue); share(client, p["Ben"], sue, [crew["id"]])
    client.delete(f"/me/plans/{sue.id}", headers=p["Ben"])
    assert client.get(f"/events/{sue.id}", headers=p["Ana"]).json()["crew_plans"] == []
    plan(client, p["Ben"], sue); share(client, p["Ben"], sue, [crew["id"]])
    assert client.post(f"/crews/{crew['id']}/leave", headers=p["Ben"]).status_code == 204
    assert client.get(f"/events/{sue.id}", headers=p["Ana"]).json()["crew_plans"] == []
    assert client.get("/me/plans", headers=p["Ben"]).json()[0]["crews"] == []
