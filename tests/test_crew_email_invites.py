"""CC-59: invites by email address: shown on sign-in, held for people who haven't registered, and never
telling the inviter whether an address has an account."""
from datetime import UTC, datetime, timedelta

from app.models import CrewEmailInvite
from app.routers import crews as crews_router

from .conftest import auth, register


def start(client):
    ana = auth(register(client, email="ana@example.com", name="Ana Cruz"))
    crew = client.post("/crews", json={"name": "Thursday Theater"}, headers=ana).json()
    crews_router.invite_limiter.reset()
    return ana, crew


def send(client, h, crew, email):
    return client.post(f"/crews/{crew['id']}/email-invites", json={"email": email}, headers=h)


def test_an_existing_member_sees_it_on_sign_in_and_accepts(client):
    ana, crew = start(client)
    ben = auth(register(client, email="ben@example.com", name="Ben Kim"))
    r = send(client, ana, crew, "  BEN@example.com ")
    assert r.status_code == 202 and r.json() == {"message": "Invite sent. We've emailed them, and they'll see it when they sign in."}
    pending = client.get("/crews/pending", headers=ben).json()
    assert [(p["crew_name"], p["invited_by"], p["members"], p["full"]) for p in pending] == [("Thursday Theater", "Ana Cruz", 1, False)]
    waiting = client.get("/crews", headers=ana).json()[0]["email_invites"]
    assert [(w["yours"], w["email"]) for w in waiting] == [(True, "ben@example.com")]
    r = client.post(f"/crews/pending/{pending[0]['id']}/accept", headers=ben)
    assert r.status_code == 200 and [m["name"] for m in r.json()["members"]] == ["Ana Cruz", "Ben Kim"]
    assert client.get("/crews/pending", headers=ben).json() == []
    assert client.get("/crews", headers=ana).json()[0]["email_invites"] == []


def test_held_for_someone_who_registers_later_and_declining_is_quiet(client):
    ana, crew = start(client)
    send(client, ana, crew, "newcomer@example.com")
    cy = auth(register(client, email="newcomer@example.com", name="Cy"))
    pending = client.get("/crews/pending", headers=cy).json()
    assert len(pending) == 1
    assert client.post(f"/crews/pending/{pending[0]['id']}/decline", headers=cy).status_code == 204
    assert client.get("/crews/pending", headers=cy).json() == []
    assert client.get("/crews", headers=ana).json()[0]["email_invites"] == []  # it just goes; nobody is told
    assert client.post(f"/crews/pending/{pending[0]['id']}/accept", headers=cy).status_code == 404


def test_the_inviter_never_learns_who_has_an_account(client):
    ana, crew = start(client)
    register(client, email="ben@example.com", name="Ben Kim")
    a = send(client, ana, crew, "ben@example.com")  # has an account
    b = send(client, ana, crew, "nobody@example.com")  # doesn't
    c = send(client, ana, crew, "ana@example.com")  # already in the crew
    d = send(client, ana, crew, "ben@example.com")  # already invited
    assert {r.status_code for r in (a, b, c, d)} == {202} and len({r.text for r in (a, b, c, d)}) == 1
    assert len(client.get("/crews", headers=ana).json()[0]["email_invites"]) == 2  # Ben and nobody, once each
    assert send(client, ana, crew, "not an email").status_code == 422


def test_only_the_invited_address_sees_it_and_only_members_can_invite(client):
    ana, crew = start(client)
    eve = auth(register(client, email="eve@example.com", name="Eve"))
    send(client, ana, crew, "ben@example.com")
    assert client.get("/crews/pending", headers=eve).json() == []
    assert send(client, eve, crew, "eve2@example.com").status_code == 404  # not a member
    invite_id = client.get("/crews", headers=ana).json()[0]["email_invites"][0]["id"]
    assert client.post(f"/crews/pending/{invite_id}/accept", headers=eve).status_code == 404
    assert client.delete(f"/crews/{crew['id']}/email-invites/{invite_id}", headers=eve).status_code == 404


def test_cancelling_and_expiry(client, db):
    ana, crew = start(client)
    ben = auth(register(client, email="ben@example.com", name="Ben Kim"))
    token = client.post(f"/crews/{crew['id']}/invites", headers=ana).json()["token"]
    client.post("/crews/join", json={"token": token}, headers=ben)
    send(client, ben, crew, "cy@example.com")
    ana_view = client.get("/crews", headers=ana).json()[0]["email_invites"][0]
    assert (ana_view["yours"], ana_view["email"], ana_view["made_by"]) == (False, None, "Ben Kim")  # the owner sees no address
    assert client.delete(f"/crews/{crew['id']}/email-invites/{ana_view['id']}", headers=ana).status_code == 204  # the owner can cancel
    send(client, ana, crew, "cy@example.com")
    db.execute(CrewEmailInvite.__table__.update().values(expires_at=datetime.now(UTC) - timedelta(minutes=1))); db.commit()
    cy = auth(register(client, email="cy@example.com", name="Cy"))
    assert client.get("/crews/pending", headers=cy).json() == []  # 14 days passed
    assert send(client, ana, crew, "cy@example.com").status_code == 202  # and it can be sent again
    assert len(client.get("/crews/pending", headers=cy).json()) == 1


def test_with_confirmed_emails_required_only_a_confirmed_address_sees_it(client, monkeypatch):
    from app.config import get_settings
    ana, crew = start(client)
    ben = auth(register(client, email="ben@example.com", name="Ben Kim"))
    send(client, ana, crew, "ben@example.com")
    monkeypatch.setattr(get_settings(), "require_verified_email", True)
    assert client.get("/crews/pending", headers=ben).json() == []


# ---- the email itself (CC-83) ------------------------------------------------------------

def test_the_invite_is_emailed_once_per_crew_while_it_waits(client, db):
    from app.mail import OUTBOX
    ana, crew = start(client)
    register(client, email="ben@example.com", name="Ben Kim")
    OUTBOX.clear()
    send(client, ana, crew, "ben@example.com")
    send(client, ana, crew, "nobody@example.com")
    send(client, ana, crew, "ana@example.com")  # already in: no email
    send(client, ana, crew, "BEN@example.com")  # already waiting: no second email
    assert [e.to for e in OUTBOX] == ["ben@example.com", "nobody@example.com"]
    a, b = OUTBOX
    assert a.subject == "Ana C. wants you to be part of their crew!" and '"Thursday Theater"' in a.text
    assert a.text.replace("ben@", "x@") == b.text.replace("nobody@", "x@")  # the same whether or not there's an account
    assert "Cruz" not in a.text and "/#account" in a.text and "14 days" in a.text

    # once it's gone (declined, cancelled, run out), a new invite emails again
    for i in db.query(CrewEmailInvite).all():
        i.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    OUTBOX.clear()
    send(client, ana, crew, "ben@example.com")
    assert [e.to for e in OUTBOX] == ["ben@example.com"]


def test_one_address_gets_at_most_three_invite_emails_a_day(client):
    from app.mail import OUTBOX
    ana, _ = start(client)
    crews = [client.post("/crews", json={"name": f"Crew {n}"}, headers=ana).json() for n in range(4)]
    OUTBOX.clear()
    for c in crews:
        assert send(client, ana, c, "cy@example.com").status_code == 202
    assert len(OUTBOX) == crews_router.INVITE_EMAILS_PER_ADDRESS_PER_DAY
    assert len(client.get("/crews", headers=ana).json()) == 5  # the fourth invite still waits, just without an email


def test_an_unconfirmed_inviter_emails_nobody_when_confirmation_is_required(client, monkeypatch):
    from app.config import get_settings
    from app.mail import OUTBOX
    ana, crew = start(client)
    monkeypatch.setattr(get_settings(), "require_verified_email", True)
    OUTBOX.clear()
    assert send(client, ana, crew, "cy@example.com").status_code == 202
    assert OUTBOX == []
