"""CC-42: changing your password (POST /auth/password)."""
from sqlalchemy import func, select

from app.models import RefreshToken
from app.routers.auth import COOKIE

from .conftest import auth

PW, NEW = "correct-horse-1", "battery-staple-2"  # register()'s default password, and a new one


def change(client, token, current=PW, new=NEW):
    return client.post("/auth/password", headers=auth(token), json={"current_password": current, "new_password": new})


def login(client, password, email="ana@example.com"):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_change_password(client, user_token):
    r = change(client, user_token)
    assert r.status_code == 200 and r.json()["user"]["email"] == "ana@example.com"
    assert client.get("/auth/me", headers=auth(r.json()["access_token"])).status_code == 200
    assert login(client, NEW).status_code == 200
    assert login(client, PW).status_code == 401


def test_other_sessions_are_signed_out_and_this_one_kept(client, user_token, db):
    login(client, PW)  # a second device signs in...
    other_device = client.cookies[COOKIE]
    login(client, PW)  # ...and a third (this tab ends up holding the newest cookie)

    assert change(client, user_token).status_code == 200
    this_tab = client.cookies[COOKIE]
    assert this_tab != other_device
    assert db.scalar(select(func.count(RefreshToken.id))) == 1  # only this tab's new session is left

    assert client.post("/auth/refresh").status_code == 200  # this tab carries on
    client.cookies.set(COOKIE, other_device, path="/auth")
    assert client.post("/auth/refresh").status_code == 401  # the other device has to sign in again


def test_wrong_current_password_changes_nothing(client, user_token, db):
    tokens_before = db.scalar(select(func.count(RefreshToken.id)))
    r = change(client, user_token, current="not-my-password")
    assert r.status_code == 403 and r.json()["detail"] == "Current password is incorrect"
    assert login(client, PW).status_code == 200
    assert db.scalar(select(func.count(RefreshToken.id))) == tokens_before + 1  # only the login above added one


def test_new_password_rules(client, user_token):
    assert change(client, user_token, new="short").status_code == 422
    assert change(client, user_token, new="é" * 40).status_code == 422  # 80 bytes: over bcrypt's 72
    assert change(client, user_token, new=PW).status_code == 400  # the same as the current one
    assert login(client, PW).status_code == 200  # none of those changed it


def test_needs_sign_in(client):
    assert client.post("/auth/password", json={"current_password": PW, "new_password": NEW}).status_code == 401


def test_password_changes_are_rate_limited(client, user_token):
    codes = [change(client, user_token, current=f"guess-{i}").status_code for i in range(11)]
    assert codes[:10] == [403] * 10 and codes[10] == 429
