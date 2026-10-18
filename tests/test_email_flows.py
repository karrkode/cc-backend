"""CC-44: confirming an email address and resetting a forgotten password."""
import logging
import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app import mail
from app.config import Settings, get_settings
from app.mail import OUTBOX
from app.models import EmailToken, RefreshToken, User
from app.routers.auth import COOKIE, FORGOT_REPLY

from .conftest import auth, register

PW, NEW = "correct-horse-1", "battery-staple-2"  # register()'s default password, and a new one


def link_token(kind, to=None):
    """The token from the latest email's #verify=/#reset= link (optionally checking who it went to)."""
    email = OUTBOX[-1]
    if to:
        assert email.to == to
    m = re.search(rf"/#{kind}=([\w-]+)", email.text)
    assert m, email.text
    return m.group(1)


def me(client, token):
    return client.get("/auth/me", headers=auth(token)).json()


def expire_all(db):
    for t in db.scalars(select(EmailToken)):
        t.expires_at = t.expires_at - timedelta(days=30)
    db.commit()


# ---- confirming an address -------------------------------------------------------------

def test_sign_up_emails_a_link_that_confirms_the_address(client, db):
    token = register(client)
    assert me(client, token)["email_verified"] is False
    assert len(OUTBOX) == 1 and OUTBOX[0].subject == "Confirm your email for Culture Crew"
    assert "Hi Ana," in OUTBOX[0].text and "expires in 24 hours" in OUTBOX[0].text
    raw = link_token("verify", to="ana@example.com")

    r = client.post("/auth/verify-email", json={"token": raw})  # no sign-in needed
    assert r.status_code == 200 and r.json()["email_verified"] is True
    assert me(client, token)["email_verified"] is True
    assert client.post("/auth/verify-email", json={"token": raw}).status_code == 400  # single use
    assert raw not in {t.token_hash for t in db.scalars(select(EmailToken))}  # only the hash is stored


def test_bad_and_expired_links(client, db):
    register(client)
    raw = link_token("verify")
    assert client.post("/auth/verify-email", json={"token": "x" * 43}).status_code == 400
    expire_all(db)
    r = client.post("/auth/verify-email", json={"token": raw})
    assert r.status_code == 400 and r.json()["detail"] == "This link has expired or was already used"


def test_resend_replaces_the_old_link(client):
    token = register(client)
    first = link_token("verify")
    r = client.post("/auth/verify-email/send", headers=auth(token))
    assert r.status_code == 200 and r.json()["detail"] == "We've sent a new link to ana@example.com."
    second = link_token("verify")
    assert first != second
    assert client.post("/auth/verify-email", json={"token": first}).status_code == 400  # replaced
    assert client.post("/auth/verify-email", json={"token": second}).status_code == 200
    assert client.post("/auth/verify-email/send", headers=auth(token)).status_code == 400  # already confirmed
    assert client.post("/auth/verify-email/send").status_code == 401


def test_changing_email_needs_confirming_again(client):
    token = register(client)
    old_link = link_token("verify")
    client.post("/auth/verify-email", json={"token": old_link})
    r = client.patch("/auth/me", headers=auth(token), json={"email": "new@example.com", "current_password": PW})
    assert r.status_code == 200 and r.json()["email_verified"] is False
    assert client.post("/auth/verify-email", json={"token": link_token("verify", to="new@example.com")}).status_code == 200


def test_a_link_only_confirms_the_address_it_was_sent_to(client):
    token = register(client)
    sent_to_old = link_token("verify")
    client.patch("/auth/me", headers=auth(token), json={"email": "new@example.com", "current_password": PW})
    assert client.post("/auth/verify-email", json={"token": sent_to_old}).status_code == 400
    assert me(client, token)["email_verified"] is False


def test_no_email_when_the_change_is_refused(client):
    token = register(client)
    register(client, email="taken@example.com", name="Other")
    OUTBOX.clear()
    r = client.patch("/auth/me", headers=auth(token), json={"email": "taken@example.com", "current_password": PW})
    assert r.status_code == 409 and OUTBOX == []
    client.patch("/auth/me", headers=auth(token), json={"display_name": "Ana B"})  # a name change sends nothing
    assert OUTBOX == []


# ---- forgotten passwords ---------------------------------------------------------------

def test_forgot_password_answers_the_same_for_any_address(client):
    register(client)
    OUTBOX.clear()
    known = client.post("/auth/forgot-password", json={"email": "Ana@Example.com"})
    unknown = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == {"detail": FORGOT_REPLY}
    assert [e.to for e in OUTBOX] == ["ana@example.com"]  # only the real account is emailed
    assert OUTBOX[0].subject == "Reset your Culture Crew password" and "expires in 60 minutes" in OUTBOX[0].text


def test_reset_password_signs_in_and_out_everywhere_else(client, db):
    register(client)
    client.post("/auth/login", json={"email": "ana@example.com", "password": PW})  # another device
    other_device = client.cookies[COOKIE]
    client.post("/auth/forgot-password", json={"email": "ana@example.com"})
    raw = link_token("reset")

    r = client.post("/auth/reset-password", json={"token": raw, "new_password": NEW})
    assert r.status_code == 200 and r.json()["user"]["email_verified"] is True  # they read its mail
    assert db.scalar(select(func.count(RefreshToken.id))) == 1  # only this new session
    assert client.post("/auth/refresh").status_code == 200
    client.cookies.set(COOKIE, other_device, path="/auth")
    assert client.post("/auth/refresh").status_code == 401
    assert client.post("/auth/login", json={"email": "ana@example.com", "password": NEW}).status_code == 200
    assert client.post("/auth/login", json={"email": "ana@example.com", "password": PW}).status_code == 401
    assert client.post("/auth/reset-password", json={"token": raw, "new_password": "another-one-3"}).status_code == 400


def test_reset_links_expire_are_replaced_and_follow_the_rules(client, db):
    register(client)
    client.post("/auth/forgot-password", json={"email": "ana@example.com"})
    first = link_token("reset")
    client.post("/auth/forgot-password", json={"email": "ana@example.com"})
    second = link_token("reset")
    assert client.post("/auth/reset-password", json={"token": first, "new_password": NEW}).status_code == 400
    assert client.post("/auth/reset-password", json={"token": second, "new_password": "short"}).status_code == 422
    expire_all(db)
    assert client.post("/auth/reset-password", json={"token": second, "new_password": NEW}).status_code == 400
    assert client.post("/auth/login", json={"email": "ana@example.com", "password": PW}).status_code == 200


def test_links_only_work_for_their_own_purpose(client):
    register(client)
    verify = link_token("verify")
    client.post("/auth/forgot-password", json={"email": "ana@example.com"})
    reset = link_token("reset")
    assert client.post("/auth/reset-password", json={"token": verify, "new_password": NEW}).status_code == 400
    assert client.post("/auth/verify-email", json={"token": reset}).status_code == 400


def test_forgot_password_is_rate_limited(client):
    codes = [client.post("/auth/forgot-password", json={"email": f"x{i}@example.com"}).status_code for i in range(11)]
    assert codes[:10] == [202] * 10 and codes[10] == 429


# ---- sending ---------------------------------------------------------------------------

def test_a_failed_email_never_breaks_sign_up(client, monkeypatch, caplog):
    def broken():
        raise RuntimeError("SES is down")
    monkeypatch.setattr(get_settings(), "email_backend", "ses")
    monkeypatch.setattr(mail, "_ses", broken)
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        token = register(client)
    assert me(client, token)["email"] == "ana@example.com"
    assert "Email to ana@example.com failed" in caplog.text


def test_where_email_goes_by_default():
    # email_backend=None: what happens when EMAIL_BACKEND isn't set (the tests themselves set it)
    assert Settings(env="local", email_backend=None).email_mode == "log"  # local: written to the log, never sent
    assert Settings(env="prod", jwt_secret="x" * 40, email_backend=None).email_mode == "none"  # prod: once configured
    assert Settings(env="prod", jwt_secret="x" * 40, email_backend="ses").email_mode == "ses"


def test_skipped_email_never_logs_the_link(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "email_backend", "none")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        mail.send(mail.Email("ana@example.com", "Reset", "https://x/#reset=SECRET-TOKEN"))
    assert "skipped" in caplog.text and "SECRET-TOKEN" not in caplog.text


# ---- requiring a confirmed address to submit (off until SES production access) ---------

EVENT = {"title": "Fresh pick", "category": "Comedy", "venue": {"name": "Annoyance", "neighborhood": "Lakeview"}}


@pytest.mark.parametrize("required", [False, True])
def test_submitting_needs_a_confirmed_address_only_when_switched_on(client, admin_token, monkeypatch, required):
    monkeypatch.setattr(get_settings(), "require_verified_email", required)
    token = register(client)
    r = client.post("/events", headers=auth(token), json=EVENT)
    assert r.status_code == (403 if required else 201)
    client.post("/auth/verify-email", json={"token": link_token("verify", to="ana@example.com")})
    assert client.post("/events", headers=auth(token), json=EVENT).status_code == 201
    assert client.post("/events", headers=auth(admin_token), json=EVENT).status_code == 201  # admins aren't held up


def test_dev_admin_counts_as_confirmed(db):
    from scripts import dev_admin
    dev_admin.ensure_dev_admin(db, "admin@example.com", "local-admin-only")
    assert db.scalar(select(User)).email_verified is True
