"""CC-187: someone the leader removes stays out until the leader invites them back, and a leaver's invites go too.
Plus the invite preview as a POST, so the token isn't in the URL."""
import pytest

from app import mail
from app.routers import crews as crews_router

from .conftest import auth, register


@pytest.fixture
def crew(client):
    """Ana leads; Ben and Cy are in it."""
    crews_router.invite_limiter.reset()
    h = {n: auth(register(client, email=f"{n.lower()}@example.com", name=n)) for n in ("Ana", "Ben", "Cy")}
    c = client.post("/crews", json={"name": "Thursday Crew"}, headers=h["Ana"]).json()
    link = make_link(client, h["Ana"], c)
    for n in ("Ben", "Cy"):
        assert client.post("/crews/join", json={"token": link["token"]}, headers=h[n]).status_code == 201
    ids = {m["name"]: m["user_id"] for m in client.get("/crews", headers=h["Ana"]).json()[0]["members"]}
    return {"id": c["id"], "h": h, "ids": ids}


def make_link(client, h, c):
    r = client.post(f"/crews/{c['id']}/invites", headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def remove(client, crew, name="Ben"):
    r = client.delete(f"/crews/{crew['id']}/members/{crew['ids'][name]}", headers=crew["h"]["Ana"])
    assert r.status_code == 200, r.text


def invite_email(client, h, crew, email):
    r = client.post(f"/crews/{crew['id']}/email-invites", json={"email": email}, headers=h)
    assert r.status_code == 202, r.text
    return r


def join(client, token, h):
    return client.post("/crews/join", json={"token": token}, headers=h)


def preview(client, token, h=None):
    return client.post("/crews/invite/preview", json={"token": token}, headers=h or {})


def members(client, crew):
    return [m["name"] for m in client.get("/crews", headers=crew["h"]["Ana"]).json()[0]["members"]]


def emails_to(address):
    return [m for m in mail.OUTBOX if m.to == address]


def test_a_removed_member_cant_rejoin_by_any_link(client, crew):
    h = crew["h"]
    leaders, others = make_link(client, h["Ana"], crew), make_link(client, h["Cy"], crew)
    bens = make_link(client, h["Ben"], crew)
    remove(client, crew)
    for link in (leaders, others):  # still live for everyone else, but not for Ben
        assert preview(client, link["token"], h["Ben"]).json()["removed"] is True
        assert preview(client, link["token"]).json()["removed"] is False  # signed out, the link says nothing
        r = join(client, link["token"], h["Ben"])
        assert r.status_code == 403 and r.json()["detail"] == crews_router.REMOVED
    assert join(client, make_link(client, h["Ana"], crew)["token"], h["Ben"]).status_code == 403  # nor a brand new one
    assert join(client, bens["token"], h["Ben"]).status_code == 404  # their own link went with them
    assert members(client, crew) == ["Ana", "Cy"]


def test_removing_someone_ends_the_invites_they_made_and_the_ones_to_them(client, crew):
    h = crew["h"]
    bens, cys = make_link(client, h["Ben"], crew), make_link(client, h["Cy"], crew)
    invite_email(client, h["Ben"], crew, "friend@example.com")
    invite_email(client, h["Cy"], crew, "other@example.com")
    invite_email(client, h["Cy"], crew, "ben@example.com")  # to a member: waits quietly (CC-184)
    remove(client, crew)
    dee = auth(register(client, email="friend@example.com", name="Dee"))
    assert preview(client, bens["token"]).status_code == 404
    assert join(client, bens["token"], dee).status_code == 404
    assert client.get("/crews/pending", headers=dee).json() == []
    assert client.get("/crews/pending", headers=h["Ben"]).json() == []
    seen = client.get("/crews", headers=h["Ana"]).json()[0]
    assert [i["id"] for i in seen["invites"] if i["id"] == bens["id"]] == []
    assert [i["id"] for i in seen["invites"] if i["id"] == cys["id"]] == [cys["id"]]  # others' links are untouched
    assert [i["made_by"] for i in seen["email_invites"]] == ["Cy"]  # to other@, and nothing to or from Ben
    assert join(client, cys["token"], dee).status_code == 201  # and Dee can still come in another way


def test_a_members_invite_cant_bring_them_back_and_doesnt_show_it(client, crew):
    h = crew["h"]
    remove(client, crew)
    before = len(emails_to("ben@example.com"))
    removed, stranger = invite_email(client, h["Cy"], crew, "ben@example.com"), invite_email(client, h["Cy"], crew, "nobody@example.com")
    assert removed.text == stranger.text  # the same answer, and the same line in the crew's list
    assert len(client.get("/crews", headers=h["Cy"]).json()[0]["email_invites"]) == 2
    assert len(emails_to("ben@example.com")) == before  # never emailed
    assert client.get("/crews/pending", headers=h["Ben"]).json() == []


def test_the_leader_invites_them_back_and_it_lifts_the_removal(client, crew):
    h = crew["h"]
    remove(client, crew)
    link = make_link(client, h["Ana"], crew)
    before = len(emails_to("ben@example.com"))
    invite_email(client, h["Ana"], crew, "ben@example.com")
    assert len(emails_to("ben@example.com")) == before + 1
    assert join(client, link["token"], h["Ben"]).status_code == 403  # the invite is the way in, not the link
    pending = client.get("/crews/pending", headers=h["Ben"]).json()
    assert [p["invited_by"] for p in pending] == ["Ana"]
    assert client.post(f"/crews/pending/{pending[0]['id']}/accept", headers=h["Ben"]).status_code == 200
    assert sorted(members(client, crew)) == ["Ana", "Ben", "Cy"]
    # back as an ordinary member: leaving and using a link works, and removing them again sticks again
    assert client.post(f"/crews/{crew['id']}/leave", headers=h["Ben"]).status_code == 204
    assert join(client, link["token"], h["Ben"]).status_code == 201
    remove(client, crew)
    assert join(client, link["token"], h["Ben"]).status_code == 403


def test_the_leaders_invite_replaces_a_members_one_already_waiting(client, crew):
    h = crew["h"]
    remove(client, crew)
    invite_email(client, h["Cy"], crew, "ben@example.com")
    before = len(emails_to("ben@example.com"))
    invite_email(client, h["Ana"], crew, "ben@example.com")
    assert len(emails_to("ben@example.com")) == before + 1
    pending = client.get("/crews/pending", headers=h["Ben"]).json()
    assert [p["invited_by"] for p in pending] == ["Ana"]
    assert client.post(f"/crews/pending/{pending[0]['id']}/accept", headers=h["Ben"]).status_code == 200


def test_the_invite_back_has_to_come_from_whoever_leads_now(client, crew):
    h = crew["h"]
    remove(client, crew)
    invite_email(client, h["Ana"], crew, "ben@example.com")
    assert client.post(f"/crews/{crew['id']}/owner", json={"user_id": crew["ids"]["Cy"]}, headers=h["Ana"]).status_code == 200
    assert client.get("/crews/pending", headers=h["Ben"]).json() == []
    invite_email(client, h["Cy"], crew, "ben@example.com")
    assert [p["invited_by"] for p in client.get("/crews/pending", headers=h["Ben"]).json()] == ["Cy"]


def test_leaving_ends_your_invites_but_isnt_a_removal(client, crew):
    h = crew["h"]
    bens, cys = make_link(client, h["Ben"], crew), make_link(client, h["Cy"], crew)
    invite_email(client, h["Ben"], crew, "friend@example.com")
    assert client.post(f"/crews/{crew['id']}/leave", headers=h["Ben"]).status_code == 204
    assert join(client, bens["token"], h["Ben"]).status_code == 404  # no link of your own to come back on
    assert client.get("/crews", headers=h["Ana"]).json()[0]["email_invites"] == []
    assert preview(client, cys["token"], h["Ben"]).json()["removed"] is False
    assert join(client, cys["token"], h["Ben"]).status_code == 201  # someone else's link still lets you back


def test_removal_is_for_that_crew_only(client, crew):
    h = crew["h"]
    other = client.post("/crews", json={"name": "Sunday Crew"}, headers=h["Ana"]).json()
    link = make_link(client, h["Ana"], other)
    remove(client, crew)
    assert join(client, link["token"], h["Ben"]).status_code == 201


def test_the_preview_takes_the_token_in_the_body(client, crew):
    h = crew["h"]
    link = make_link(client, h["Ana"], crew)
    seen = preview(client, link["token"]).json()
    assert (seen["crew_name"], seen["invited_by"], seen["members"], seen["already_member"], seen["full"]) == ("Thursday Crew", "Ana", 3, False, False)
    assert preview(client, link["token"], h["Cy"]).json()["already_member"] is True
    assert preview(client, "x" * 32).status_code == 404
    assert client.post("/crews/invite/preview", json={}).status_code == 422


def test_a_token_in_the_path_gets_no_answer(client, crew):
    link = make_link(client, crew["h"]["Ana"], crew)
    assert client.get(f"/crews/invite/{link['token']}").status_code == 404
