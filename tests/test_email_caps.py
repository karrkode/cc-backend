"""CC-184: a bounded number of emails to any one address and from any one account, whatever the route. Over a cap
the answer is the same as usual and nothing is sent."""
import re

from sqlalchemy import select

from app import emailcaps
from app.mail import OUTBOX
from app.ratelimit import auth_limiter
from app.models import SentEmail, User
from app.routers import crews as crews_router
from app.routers.auth import FORGOT_REPLY

from .conftest import age_sent_emails, auth, register

PW_DEFAULT = "correct-horse-1"  # register()'s default password


def to(address):
    return [e for e in OUTBOX if e.to == address]


def crew_of(client, h, name="Thursday Theater"):
    crews_router.invite_limiter.reset()
    return client.post("/crews", json={"name": name}, headers=h).json()


def invite(client, h, crew, email):
    r = client.post(f"/crews/{crew['id']}/email-invites", json={"email": email}, headers=h)
    assert r.status_code == 202 and r.json() == {"message": crews_router.SENT}
    return r


def waiting(client, h):
    return [w["email"] for w in client.get("/crews", headers=h).json()[0]["email_invites"]]


# ---- one address, every kind of email together ------------------------------------------------

def test_register_still_makes_the_account_but_sends_nothing_once_the_address_has_had_three(client):
    ana = auth(register(client))
    crews = [crew_of(client, ana, f"Crew {n}") for n in range(3)]
    for c in crews:
        invite(client, ana, c, "cy@example.com")
    assert len(to("cy@example.com")) == emailcaps.PER_ADDRESS_PER_HOUR
    r = client.post("/auth/register", json={"email": "cy@example.com", "password": PW_DEFAULT, "display_name": "Cy"})
    assert r.status_code == 201 and r.json()["user"]["email"] == "cy@example.com"  # the usual answer
    assert len(to("cy@example.com")) == 3  # but no confirm email


def test_resend_confirm_once_a_minute_and_three_an_hour(client, db):
    h = auth(register(client))  # 1: the sign-up email
    reply = client.post("/auth/verify-email/send", headers=h)
    assert reply.status_code == 200 and len(to("ana@example.com")) == 1  # within the minute: the same answer, no email
    for sent in (2, 3):
        age_sent_emails(db, minutes=2)
        assert client.post("/auth/verify-email/send", headers=h).json() == reply.json()
        assert len(to("ana@example.com")) == sent
    age_sent_emails(db, minutes=2)
    assert client.post("/auth/verify-email/send", headers=h).json() == reply.json()
    assert len(to("ana@example.com")) == 3  # the hour's three
    age_sent_emails(db, hours=1)
    client.post("/auth/verify-email/send", headers=h)
    assert len(to("ana@example.com")) == 4


def test_a_capped_resend_keeps_the_last_link_working(client):
    h = auth(register(client))
    link = re.search(r"#verify=([\w-]+)", OUTBOX[-1].text).group(1)
    client.post("/auth/verify-email/send", headers=h)  # within the minute: nothing new, nothing replaced
    assert client.post("/auth/verify-email", json={"token": link}).status_code == 200


def test_forgot_password_answers_the_same_and_sends_at_most_three_an_hour(client, db):
    register(client)
    OUTBOX.clear()
    age_sent_emails(db, hours=2)  # the sign-up email is out of the way
    replies = []
    for _ in range(5):
        r = client.post("/auth/forgot-password", json={"email": "ana@example.com"})
        replies.append((r.status_code, r.json()["detail"]))
        age_sent_emails(db, minutes=2)
    assert set(replies) == {(202, FORGOT_REPLY)}
    assert len(to("ana@example.com")) == 3
    last = re.search(r"#reset=([\w-]+)", OUTBOX[-1].text).group(1)
    r = client.post("/auth/reset-password", json={"token": last, "new_password": "battery-staple-2"})
    assert r.status_code == 200  # the capped requests didn't replace the last link


def test_forgot_password_twice_in_a_minute_sends_once(client, db):
    register(client)
    OUTBOX.clear()
    age_sent_emails(db, hours=2)
    client.post("/auth/forgot-password", json={"email": "ana@example.com"})
    client.post("/auth/forgot-password", json={"email": "ana@example.com"})
    assert len(OUTBOX) == 1


def test_email_change_to_an_address_that_had_its_three_changes_but_sends_nothing(client):
    ana = auth(register(client))
    for c in [crew_of(client, ana, f"Crew {n}") for n in range(3)]:
        invite(client, ana, c, "target@example.com")
    ben = auth(register(client, email="ben@example.com", name="Ben"))
    r = client.patch("/auth/me", headers=ben, json={"email": "target@example.com", "current_password": PW_DEFAULT})
    assert r.status_code == 200 and r.json()["email"] == "target@example.com" and r.json()["email_verified"] is False
    assert len(to("target@example.com")) == 3


def test_ten_a_day_to_one_address(client, db):
    h = auth(register(client))  # 1
    for n in range(2, 12):
        age_sent_emails(db, hours=1, minutes=1)  # always a new hour, never a new day
        client.post("/auth/verify-email/send", headers=h)
        assert len(to("ana@example.com")) == min(n, emailcaps.PER_ADDRESS_PER_DAY)
    age_sent_emails(db, hours=24)
    auth_limiter.reset()  # the per-IP limit of ten a minute
    client.post("/auth/verify-email/send", headers=h)
    assert len(to("ana@example.com")) == 11


def test_email_change_counts_against_the_new_address(client, db):
    h = auth(register(client))
    for n in range(3):
        age_sent_emails(db, minutes=2)
        r = client.patch("/auth/me", headers=h, json={"email": f"ana{n % 2}@example.com", "current_password": PW_DEFAULT})
        assert r.status_code == 200
    assert [len(to(a)) for a in ("ana0@example.com", "ana1@example.com")] == [3, 2]  # CC-203: + the moved-away notice
    rows = db.scalars(select(SentEmail.email).where(SentEmail.kind == emailcaps.VERIFY).order_by(SentEmail.id)).all()
    assert rows == ["ana@example.com", "ana0@example.com", "ana1@example.com", "ana0@example.com"]
    notices = db.scalars(select(SentEmail.email).where(SentEmail.kind == emailcaps.NOTICE).order_by(SentEmail.id)).all()
    assert notices == ["ana@example.com", "ana0@example.com", "ana1@example.com"]


# ---- crew invites ---------------------------------------------------------------------------

def test_cancel_and_reinvite_sends_no_more_than_three(client):
    ana = auth(register(client))
    crew = crew_of(client, ana)
    for _ in range(6):
        invite(client, ana, crew, "flood@example.net")
        invite_id = client.get("/crews", headers=ana).json()[0]["email_invites"][0]["id"]
        assert client.delete(f"/crews/{crew['id']}/email-invites/{invite_id}", headers=ana).status_code == 204
    assert len(to("flood@example.net")) == emailcaps.INVITES_TO_ADDRESS_PER_DAY


def test_cancel_and_reinvite_stays_capped_after_the_hour(client, db):
    ana = auth(register(client))
    crew = crew_of(client, ana)
    for _ in range(3):
        age_sent_emails(db, hours=1, minutes=1)
        invite(client, ana, crew, "flood@example.net")
        invite_id = client.get("/crews", headers=ana).json()[0]["email_invites"][0]["id"]
        client.delete(f"/crews/{crew['id']}/email-invites/{invite_id}", headers=ana)
    age_sent_emails(db, hours=1, minutes=1)
    invite(client, ana, crew, "flood@example.net")
    assert len(to("flood@example.net")) == 3  # three crew invites a day to one address, cancelled or not


def test_ten_invite_emails_an_hour_per_account(client):
    ana = auth(register(client))
    crew = crew_of(client, ana)
    OUTBOX.clear()
    for n in range(11):
        invite(client, ana, crew, f"friend{n}@example.com")
    assert len(OUTBOX) == emailcaps.INVITES_PER_ACCOUNT_PER_HOUR == 10
    assert len(waiting(client, ana)) == 11  # the eleventh still waits for its address, without an email
    ben = auth(register(client, email="ben@example.com", name="Ben"))  # someone else's invites aren't held back
    ben_crew = crew_of(client, ben, "Ben's crew")
    invite(client, ben, ben_crew, "friend10@example.com")
    assert [e.to for e in OUTBOX][-1] == "friend10@example.com"


def test_twenty_invite_emails_a_day_per_account(client, db):
    ana = auth(register(client))
    crew = crew_of(client, ana)
    OUTBOX.clear()
    for n in range(25):
        if n % 10 == 0:
            age_sent_emails(db, hours=1, minutes=1)
            crews_router.invite_limiter.reset()
        invite(client, ana, crew, f"friend{n}@example.com")
    assert len(OUTBOX) == emailcaps.INVITES_PER_ACCOUNT_PER_DAY == 20
    age_sent_emails(db, hours=24)
    invite(client, ana, crew, "friend25@example.com")
    assert len(OUTBOX) == 21


def test_an_unconfirmed_inviter_records_nothing(client, db, monkeypatch):
    from app.config import get_settings
    ana = auth(register(client))
    crew = crew_of(client, ana)
    db.query(SentEmail).delete(); db.commit()
    monkeypatch.setattr(get_settings(), "require_verified_email", True)
    invite(client, ana, crew, "cy@example.com")
    assert db.scalars(select(SentEmail)).all() == []  # nothing sent, so nothing counts against cy


def test_the_inviter_sees_the_same_whether_or_not_the_address_is_in_the_crew(client):
    ana = auth(register(client, name="Ana Cruz"))
    crew = crew_of(client, ana)
    ben = auth(register(client, email="ben@example.com", name="Ben Kim"))
    token = client.post(f"/crews/{crew['id']}/invites", headers=ana).json()["token"]
    client.post("/crews/join", json={"token": token}, headers=ben)
    OUTBOX.clear()
    a = invite(client, ana, crew, "ben@example.com")  # already in the crew
    b = invite(client, ana, crew, "cy@example.com")  # not
    assert a.text == b.text
    assert waiting(client, ana) == ["ben@example.com", "cy@example.com"]
    assert [e.to for e in OUTBOX] == ["cy@example.com"]  # but someone already in isn't emailed
    assert client.get("/crews/pending", headers=ben).json() == []  # nor shown it
    assert [w["email"] for w in client.get("/crews", headers=ben).json()[0]["email_invites"]] == [None, None]


def test_deleting_an_account_keeps_its_sent_emails(client, db):
    register(client)
    db.query(User).delete(); db.commit()
    assert [(e.email, e.account_id) for e in db.scalars(select(SentEmail))] == [("ana@example.com", None)]
