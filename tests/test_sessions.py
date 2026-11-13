"""CC-203: sessions end at once when they should, last at most max_session_days, and changes are announced."""
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select

from app import emailcaps
from app.config import get_settings
from app.mail import OUTBOX
from app.models import RefreshToken, SentEmail, User
from app.routers.auth import COOKIE

from .conftest import auth, register, set_refresh_cookie

PW = "correct-horse-1"


def _second_device(client):
    """Sign in again as Ana, as if on another device: (access token, refresh cookie)."""
    r = client.post("/auth/login", json={"email": "ana@example.com", "password": PW})
    assert r.status_code == 200
    return r.json()["access_token"], r.cookies[COOKIE]


def _reset_token():
    return OUTBOX[-1].text.split("#reset=")[1].split()[0]


# ---- immediate sign-out ---------------------------------------------------------------------

def test_access_tokens_carry_the_session_version(client, user_token):
    s = get_settings()
    claims = jwt.decode(user_token, s.jwt_secret, algorithms=["HS256"], issuer=s.jwt_issuer)
    assert claims["sv"] == 0


def test_tokens_from_before_the_deploy_still_work(client, user_token):
    """A token without "sv" counts as version 0, so nobody is signed out when CC-203 ships."""
    s = get_settings()
    claims = jwt.decode(user_token, s.jwt_secret, algorithms=["HS256"], issuer=s.jwt_issuer)
    del claims["sv"]
    old = jwt.encode(claims, s.jwt_secret, algorithm="HS256")
    assert client.get("/auth/me", headers=auth(old)).status_code == 200


def test_sign_out_everywhere_ends_every_session_at_once(client, user_token):
    other_access, other_cookie = _second_device(client)
    assert client.post("/auth/logout-everywhere", headers=auth(user_token)).status_code == 204
    for token in (user_token, other_access):  # access tokens stop working now, not in 15 minutes
        assert client.get("/auth/me", headers=auth(token)).status_code == 401
    assert COOKIE not in client.cookies  # this device's cookie is cleared
    set_refresh_cookie(client, other_cookie)
    assert client.post("/auth/refresh").status_code == 401
    # signing in again works, with the new version
    fresh, _ = _second_device(client)
    assert client.get("/auth/me", headers=auth(fresh)).status_code == 200


def test_sign_out_everywhere_needs_a_sign_in(client):
    assert client.post("/auth/logout-everywhere").status_code == 401


def test_password_change_ends_other_access_tokens_and_keeps_this_one(client, user_token):
    other_access, _ = _second_device(client)
    r = client.post("/auth/password", headers=auth(user_token),
                    json={"current_password": PW, "new_password": "new-horse-battery-2"})
    assert r.status_code == 200
    assert client.get("/auth/me", headers=auth(other_access)).status_code == 401
    assert client.get("/auth/me", headers=auth(user_token)).status_code == 401  # the old one too
    assert client.get("/auth/me", headers=auth(r.json()["access_token"])).status_code == 200


def test_password_reset_ends_existing_access_tokens(client, user_token):
    assert client.post("/auth/forgot-password", json={"email": "ana@example.com"}).status_code == 202
    r = client.post("/auth/reset-password", json={"token": _reset_token(), "new_password": "new-horse-battery-2"})
    assert r.status_code == 200
    assert client.get("/auth/me", headers=auth(user_token)).status_code == 401
    assert client.get("/auth/me", headers=auth(r.json()["access_token"])).status_code == 200


def test_reusing_a_stolen_refresh_token_ends_access_tokens_too(client, user_token, db):
    stolen = client.cookies[COOKIE]
    current = client.post("/auth/refresh").json()["access_token"]
    for t in db.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_not(None))):
        t.revoked_at -= timedelta(minutes=5)
    db.commit()
    set_refresh_cookie(client, stolen)
    assert client.post("/auth/refresh").status_code == 401
    assert client.get("/auth/me", headers=auth(current)).status_code == 401


# ---- maximum session length -----------------------------------------------------------------

def _age_session(db, days):
    for t in db.scalars(select(RefreshToken)):
        t.signed_in_at -= timedelta(days=days)
    db.commit()


def test_refresh_keeps_the_original_sign_in_time(client, user_token, db):
    first = db.scalar(select(RefreshToken)).signed_in_at
    for _ in range(3):
        assert client.post("/auth/refresh").status_code == 200
    db.expire_all()
    assert {t.signed_in_at for t in db.scalars(select(RefreshToken))} == {first}


def test_a_session_ends_after_the_maximum_length(client, user_token, db):
    days = get_settings().max_session_days
    _age_session(db, days - 1)
    r = client.post("/auth/refresh")
    assert r.status_code == 200  # still inside: but the new cookie lasts only the day that's left
    assert "Max-Age=" in r.headers["set-cookie"]
    max_age = int(r.headers["set-cookie"].split("Max-Age=")[1].split(";")[0])
    assert 86400 - 60 <= max_age <= 86400
    token = db.scalar(select(RefreshToken).where(RefreshToken.revoked_at.is_(None)))
    expires = token.expires_at if token.expires_at.tzinfo else token.expires_at.replace(tzinfo=UTC)
    assert expires - datetime.now(UTC) <= timedelta(days=1)

    _age_session(db, 1)  # now past the maximum, however recently it refreshed
    assert client.post("/auth/refresh").status_code == 401
    # a sign-in with the password starts a new session
    assert client.post("/auth/login", json={"email": "ana@example.com", "password": PW}).status_code == 200


# ---- change notices -------------------------------------------------------------------------

def test_password_change_emails_a_notice(client, user_token):
    OUTBOX.clear()
    client.post("/auth/password", headers=auth(user_token),
                json={"current_password": PW, "new_password": "new-horse-battery-2"})
    [notice] = OUTBOX
    assert notice.to == "ana@example.com"
    assert notice.subject == "Your Chi Culture Crew password was changed"
    assert "changed" in notice.text and "info@chiculturecrew.com" in notice.text


def test_password_reset_emails_a_notice(client, user_token):
    client.post("/auth/forgot-password", json={"email": "ana@example.com"})
    client.post("/auth/reset-password", json={"token": _reset_token(), "new_password": "new-horse-battery-2"})
    notice = OUTBOX[-1]
    assert notice.to == "ana@example.com" and notice.subject == "Your Chi Culture Crew password was changed"
    assert "was just reset" in notice.text


def test_email_change_tells_the_old_address(client, user_token):
    OUTBOX.clear()
    r = client.patch("/auth/me", headers=auth(user_token),
                     json={"email": "ana.new@example.com", "current_password": PW})
    assert r.status_code == 200
    by_to = {e.to: e for e in OUTBOX}
    assert set(by_to) == {"ana.new@example.com", "ana@example.com"}  # confirm link, and the notice
    notice = by_to["ana@example.com"]
    assert notice.subject == "The email for your Chi Culture Crew account was changed"
    assert "ana.new@example.com" not in notice.text  # never tells the old address where the account went


def test_a_name_change_sends_no_notice(client, user_token):
    OUTBOX.clear()
    assert client.patch("/auth/me", headers=auth(user_token), json={"display_name": "Ana B"}).status_code == 200
    assert OUTBOX == []


def test_notices_count_against_the_caps(client, user_token, db):
    for _ in range(emailcaps.PER_ADDRESS_PER_HOUR):
        db.add(SentEmail(email="ana@example.com", kind=emailcaps.VERIFY, sent_at=datetime.now(UTC)))
    db.commit()
    OUTBOX.clear()
    r = client.post("/auth/password", headers=auth(user_token),
                    json={"current_password": PW, "new_password": "new-horse-battery-2"})
    assert r.status_code == 200 and OUTBOX == []  # the change still happens; the email is capped
    assert db.scalar(select(User)).session_version == 1
