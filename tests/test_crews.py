"""CC-74: culture crews: start one, invite with a link, join, leave, and keep members private."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import CrewInvite
from app.routers import crews as crews_router

from .conftest import auth, register


def people(client):
    """Three signed-up people: Ana (starts the crew), Ben and Cy."""
    return {n: auth(register(client, email=f"{n.lower()}@example.com", name=n)) for n in ("Ana", "Ben", "Cy")}


def start(client, h, name="Thursday Crew"):
    r = client.post("/crews", json={"name": f"  {name} ", "description": "Plays and a drink after"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def invite(client, h, crew_id):
    r = client.post(f"/crews/{crew_id}/invites", headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def test_start_invite_join_and_see_names_never_emails(client):
    p = people(client)
    crew = start(client, p["Ana"])
    assert (crew["name"], crew["owner"], [m["name"] for m in crew["members"]]) == ("Thursday Crew", True, ["Ana"])
    link = invite(client, p["Ana"], crew["id"])
    assert link["url"].endswith("/#join=" + link["token"])
    preview = client.get(f"/crews/invite/{link['token']}").json()  # works before signing in
    assert preview == {"crew_id": crew["id"], "crew_name": "Thursday Crew", "description": "Plays and a drink after",
                       "invited_by": "Ana", "members": 1, "already_member": False, "full": False}
    r = client.post("/crews/join", json={"token": link["token"]}, headers=p["Ben"])
    assert r.status_code == 201 and [m["name"] for m in r.json()["members"]] == ["Ana", "Ben"]
    assert client.post("/crews/join", json={"token": link["token"]}, headers=p["Ben"]).status_code == 200  # again: fine
    seen = client.get("/crews", headers=p["Ben"]).json()
    assert len(seen) == 1 and "example.com" not in str(seen) and seen[0]["owner"] is False
    assert seen[0]["members"][1]["you"] is True
    assert client.post("/crews/join", json={"token": link["token"]}).status_code == 401  # signing in first


def test_outsiders_cant_see_or_touch_a_crew(client):
    p = people(client)
    crew = start(client, p["Ana"])
    assert client.get("/crews", headers=p["Cy"]).json() == []
    for method, path in (("post", f"/crews/{crew['id']}/invites"), ("patch", f"/crews/{crew['id']}"), ("delete", f"/crews/{crew['id']}"),
                         ("post", f"/crews/{crew['id']}/leave")):
        kw = {"json": {"name": "Mine now"}} if method == "patch" else {}
        assert getattr(client, method)(path, headers=p["Cy"], **kw).status_code == 404, path


def test_members_invite_owner_manages(client):
    p = people(client)
    crew = start(client, p["Ana"])
    client.post("/crews/join", json={"token": invite(client, p["Ana"], crew["id"])["token"]}, headers=p["Ben"])
    bens = invite(client, p["Ben"], crew["id"])  # any member can invite
    client.post("/crews/join", json={"token": bens["token"]}, headers=p["Cy"])
    cid = crew["id"]
    assert client.patch(f"/crews/{cid}", json={"name": "Ben's crew"}, headers=p["Ben"]).status_code == 403
    assert client.delete(f"/crews/{cid}/invites/{bens['id']}", headers=p["Cy"]).status_code == 403  # not Cy's link
    assert client.delete(f"/crews/{cid}/invites/{bens['id']}", headers=p["Ana"]).status_code == 204  # the owner can
    assert client.get(f"/crews/invite/{bens['token']}").status_code == 404
    ben_id = next(m["user_id"] for m in client.get("/crews", headers=p["Ben"]).json()[0]["members"] if m["name"] == "Ben")
    cy_id = next(m["user_id"] for m in client.get("/crews", headers=p["Cy"]).json()[0]["members"] if m["name"] == "Cy")
    r = client.delete(f"/crews/{cid}/members/{cy_id}", headers=p["Ana"])
    assert [m["name"] for m in r.json()["members"]] == ["Ana", "Ben"] and client.get("/crews", headers=p["Cy"]).json() == []
    assert client.post(f"/crews/{cid}/leave", headers=p["Ana"]).status_code == 409  # the owner hands over first
    r = client.post(f"/crews/{cid}/owner", json={"user_id": ben_id}, headers=p["Ana"])
    assert r.status_code == 200 and r.json()["owner"] is False
    assert client.post(f"/crews/{cid}/leave", headers=p["Ana"]).status_code == 204
    assert client.patch(f"/crews/{cid}", json={"name": "Ben's crew"}, headers=p["Ben"]).json()["name"] == "Ben's crew"
    assert client.delete(f"/crews/{cid}", headers=p["Ben"]).status_code == 204
    assert client.get("/crews", headers=p["Ben"]).json() == []


def test_links_expire_and_crews_fill_up(client, db, monkeypatch):
    p = people(client)
    crew = start(client, p["Ana"])
    link = invite(client, p["Ana"], crew["id"])
    db.execute(CrewInvite.__table__.update().values(expires_at=datetime.now(UTC) - timedelta(minutes=1))); db.commit()
    assert client.post("/crews/join", json={"token": link["token"]}, headers=p["Ben"]).status_code == 404
    monkeypatch.setattr(crews_router, "MAX_MEMBERS", 2)
    link = invite(client, p["Ana"], crew["id"])
    client.post("/crews/join", json={"token": link["token"]}, headers=p["Ben"])
    assert client.get(f"/crews/invite/{link['token']}").json()["full"] is True
    assert client.post("/crews/join", json={"token": link["token"]}, headers=p["Cy"]).status_code == 409
    assert client.post(f"/crews/{crew['id']}/invites", headers=p["Ana"]).status_code == 409
    stored = db.scalars(select(CrewInvite.token_hash)).all()
    assert link["token"] not in stored  # only a hash is kept


def test_invite_links_are_rate_limited(client, monkeypatch):
    p = people(client)
    crew = start(client, p["Ana"])
    crews_router.invite_limiter.reset()
    monkeypatch.setattr(crews_router, "INVITES_PER_HOUR", 2)
    codes = [client.post(f"/crews/{crew['id']}/invites", headers=p["Ana"]).status_code for _ in range(3)]
    assert codes == [201, 201, 429]
    crews_router.invite_limiter.reset()
