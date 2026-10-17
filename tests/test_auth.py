from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings
from app.routers.auth import COOKIE

from .conftest import auth, register


def test_register_signs_you_in(client):
    r = client.post("/auth/register", json={"email": "Ana@Example.com", "password": "correct-horse-1"})
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "ana@example.com"  # stored lower-case
    assert body["user"]["role"] == "user"
    assert "password_hash" not in body["user"]
    assert COOKIE in r.cookies
    cookie_header = r.headers["set-cookie"]
    assert "HttpOnly" in cookie_header and "Path=/auth" in cookie_header and "samesite=lax" in cookie_header.lower()

    me = client.get("/auth/me", headers=auth(body["access_token"]))
    assert me.status_code == 200 and me.json()["email"] == "ana@example.com"


def test_duplicate_email_is_rejected_regardless_of_case(client):
    register(client, email="ana@example.com")
    r = client.post("/auth/register", json={"email": "ANA@example.com", "password": "another-pass-1"})
    assert r.status_code == 409


def test_password_rules(client):
    assert client.post("/auth/register", json={"email": "a@example.com", "password": "short"}).status_code == 422
    assert client.post("/auth/register", json={"email": "a@example.com", "password": "é" * 37}).status_code == 422
    assert client.post("/auth/register", json={"email": "not-an-email", "password": "long-enough-1"}).status_code == 422


def test_login(client):
    register(client, email="ana@example.com", password="correct-horse-1")
    client.cookies.clear()
    ok = client.post("/auth/login", json={"email": "ANA@example.com", "password": "correct-horse-1"})
    assert ok.status_code == 200 and ok.json()["access_token"]

    wrong = client.post("/auth/login", json={"email": "ana@example.com", "password": "wrong-password"})
    nobody = client.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"})
    assert wrong.status_code == nobody.status_code == 401
    assert wrong.json() == nobody.json()  # same message either way: doesn't reveal which emails exist


def test_protected_routes_need_a_valid_token(client, user_token):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers=auth("garbage")).status_code == 401
    assert client.get("/auth/me", headers=auth(user_token + "x")).status_code == 401  # bad signature

    s = get_settings()
    claims = jwt.decode(user_token, s.jwt_secret, algorithms=["HS256"], issuer=s.jwt_issuer)
    claims["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    expired = jwt.encode(claims, s.jwt_secret, algorithm="HS256")
    assert client.get("/auth/me", headers=auth(expired)).status_code == 401

    claims["exp"] = datetime.now(UTC) + timedelta(minutes=5)
    forged = jwt.encode(claims, "someone-elses-secret", algorithm="HS256")
    assert client.get("/auth/me", headers=auth(forged)).status_code == 401


def test_refresh_rotates_the_cookie(client, user_token):
    first = client.cookies[COOKIE]
    r = client.post("/auth/refresh")
    assert r.status_code == 200
    second = client.cookies[COOKIE]
    assert second != first
    assert client.get("/auth/me", headers=auth(r.json()["access_token"])).status_code == 200


def test_reusing_an_old_refresh_token_ends_every_session(client, user_token):
    stolen = client.cookies[COOKIE]
    assert client.post("/auth/refresh").status_code == 200  # the real user refreshes
    current = client.cookies[COOKIE]

    client.cookies.set(COOKIE, stolen, path="/auth")  # an attacker replays the old one
    assert client.post("/auth/refresh").status_code == 401

    client.cookies.set(COOKIE, current, path="/auth")  # so the real user's session is ended too
    assert client.post("/auth/refresh").status_code == 401


def test_logout_revokes_the_refresh_token(client, user_token):
    token = client.cookies[COOKIE]
    assert client.post("/auth/logout").status_code == 204
    client.cookies.set(COOKIE, token, path="/auth")
    assert client.post("/auth/refresh").status_code == 401


def test_refresh_without_cookie(client):
    assert client.post("/auth/refresh").status_code == 401


def test_login_is_rate_limited(client):
    limit = get_settings().login_attempts_per_minute
    codes = [client.post("/auth/login", json={"email": "x@example.com", "password": "wrong-password"}).status_code
             for _ in range(limit + 1)]
    assert codes[:limit] == [401] * limit
    assert codes[-1] == 429
