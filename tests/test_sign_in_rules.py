"""CC-191: failed sign-ins limited per account, common passwords refused when one is set, passwords too long
for bcrypt treated as wrong, password changes cancelling reset links, and forwarded addresses trusted only
from the proxy."""
import asyncio
import re
from pathlib import Path

import bcrypt
import pytest
from sqlalchemy import select
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import security
from app.config import get_settings
from app.mail import OUTBOX
from app.models import User
from app.passwords import TOO_COMMON, is_common
from app.ratelimit import auth_limiter, login_failures
from app.security import hash_password

from .conftest import auth, register

PW, NEW = "correct-horse-1", "battery-staple-2"  # register()'s default password, and a new one
LONG = "a-long-passphrase-" * 5  # 90 bytes: over bcrypt's 72


def login(client, password, email="ana@example.com"):
    return client.post("/auth/login", json={"email": email, "password": password})


def fail_from_many_addresses(client, email, times):
    """Wrong passwords, as if each came from a different address (the per-address limit never trips)."""
    for _ in range(times):
        auth_limiter.reset()
        assert login(client, "not-the-password", email).status_code == 401


def reset_link(client, email="ana@example.com"):
    assert client.post("/auth/forgot-password", json={"email": email}).status_code == 202
    return re.search(r"/#reset=([\w-]+)", OUTBOX[-1].text).group(1)


# ---- failed sign-ins per account ----------------------------------------------------------

def test_an_account_is_held_back_after_too_many_failures_from_anywhere(client):
    register(client)
    limit = get_settings().login_failures_per_account
    fail_from_many_addresses(client, "ana@example.com", limit)
    auth_limiter.reset()
    r = login(client, PW, "ANA@example.com")  # even the right password, in any case, waits it out
    assert r.status_code == 429 and "15 minutes" in r.json()["detail"]
    assert login(client, PW, "bea@example.com").status_code == 401  # other accounts aren't affected


def test_an_address_without_an_account_gets_the_same_replies(client):
    register(client)
    limit = get_settings().login_failures_per_account
    fail_from_many_addresses(client, "ana@example.com", limit)
    fail_from_many_addresses(client, "nobody@example.com", limit)
    auth_limiter.reset()
    real, made_up = login(client, "guess", "ana@example.com"), login(client, "guess", "nobody@example.com")
    assert real.status_code == made_up.status_code == 429
    assert real.json() == made_up.json()


def test_failures_expire_and_a_good_sign_in_clears_them(client):
    register(client)
    limit = get_settings().login_failures_per_account
    fail_from_many_addresses(client, "ana@example.com", limit - 1)
    auth_limiter.reset()
    assert login(client, PW).status_code == 200  # a typo or nine, then the right one: counting starts again
    fail_from_many_addresses(client, "ana@example.com", limit - 1)
    auth_limiter.reset()
    assert login(client, "one-more-typo").status_code == 401  # 10 now, so held back...
    assert login(client, PW).status_code == 429
    for q in login_failures._fails.values():  # ...until the window has passed
        for i in range(len(q)):
            q[i] -= login_failures.window + 1
    assert login(client, PW).status_code == 200


def test_resetting_the_password_lets_the_account_sign_in_again(client):
    register(client)
    fail_from_many_addresses(client, "ana@example.com", get_settings().login_failures_per_account)
    auth_limiter.reset()
    token = reset_link(client)
    assert client.post("/auth/reset-password", json={"token": token, "new_password": NEW}).status_code == 200
    assert login(client, NEW).status_code == 200


def test_many_accounts_dont_grow_the_count_without_bound(client, monkeypatch):
    monkeypatch.setattr(login_failures, "PRUNE_AT", 5)
    for i in range(5):
        login_failures.record(f"old{i}@example.com")
    for q in login_failures._fails.values():
        q[0] -= login_failures.window + 1
    login_failures.record("new@example.com")
    assert list(login_failures._fails) == ["new@example.com"]


# ---- common passwords ---------------------------------------------------------------------

def test_the_list_is_bundled_and_ignores_case():
    assert is_common("password1") and is_common("PassWord1") and is_common("iloveyou")
    assert not is_common(PW) and not is_common(NEW)


def detail(r):
    return r.json()["detail"][0]["msg"]


def test_sign_up_refuses_a_common_password(client):
    r = client.post("/auth/register", json={"email": "ana@example.com", "password": "Password1"})
    assert r.status_code == 422 and TOO_COMMON in detail(r)


def test_changing_to_a_common_password_is_refused(client, user_token):
    r = client.post("/auth/password", headers=auth(user_token), json={"current_password": PW, "new_password": "iloveyou"})
    assert r.status_code == 422 and TOO_COMMON in detail(r)
    assert login(client, PW).status_code == 200  # unchanged


def test_a_reset_to_a_common_password_is_refused_and_the_link_still_works(client):
    register(client)
    token = reset_link(client)
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "qwertyuiop"})
    assert r.status_code == 422 and TOO_COMMON in detail(r)
    assert client.post("/auth/reset-password", json={"token": token, "new_password": NEW}).status_code == 200


def test_an_existing_common_password_still_signs_in(client, db):
    register(client)
    user = db.scalar(select(User).where(User.email == "ana@example.com"))
    user.password_hash = hash_password("password1")  # set before CC-191
    db.commit()
    assert login(client, "password1").status_code == 200


# ---- passwords over 72 bytes --------------------------------------------------------------

@pytest.fixture
def bcrypt_calls(monkeypatch):
    calls = []
    real = bcrypt.checkpw

    def counting(pw, hashed):
        calls.append(len(pw))
        return real(pw, hashed)
    monkeypatch.setattr(security.bcrypt, "checkpw", counting)
    return calls


@pytest.mark.parametrize("password", [LONG, "é" * 40, "x" * 1000])
def test_a_long_password_at_sign_in_is_just_incorrect(client, bcrypt_calls, password):
    register(client)
    bcrypt_calls.clear()
    r = login(client, password)
    assert r.status_code == 401 and r.json() == login(client, "wrong-password").json()
    assert bcrypt_calls[0] <= 72  # it still did one full bcrypt check, so it takes as long as any other


def test_a_long_current_password_is_just_incorrect(client, user_token, bcrypt_calls):
    r = client.post("/auth/password", headers=auth(user_token), json={"current_password": LONG, "new_password": NEW})
    assert r.status_code == 403 and r.json()["detail"] == "Current password is incorrect"
    r = client.patch("/auth/me", headers=auth(user_token), json={"email": "new@example.com", "current_password": LONG})
    assert r.status_code == 403 and r.json()["detail"] == "Current password is incorrect"
    assert len(bcrypt_calls) == 2


# ---- a password change cancels reset links ------------------------------------------------

def test_changing_the_password_cancels_unused_reset_links(client, user_token):
    token = reset_link(client)
    r = client.post("/auth/password", headers=auth(user_token), json={"current_password": PW, "new_password": NEW})
    assert r.status_code == 200
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "third-password-3"})
    assert r.status_code == 400
    assert login(client, NEW).status_code == 200


# ---- forwarded addresses ------------------------------------------------------------------

def trusted_ranges():
    cmd = (Path(__file__).parent.parent / "Dockerfile").read_text()
    return re.search(r'"--forwarded-allow-ips",\s*"([^"]+)"', cmd).group(1)


def client_seen(peer):
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]
    scope = {"type": "http", "client": (peer, 5000), "scheme": "http",
             "headers": [(b"x-forwarded-for", b"198.51.100.7")]}
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted_ranges())(scope, None, None))
    return seen["client"]


def test_forwarded_addresses_count_only_from_the_proxy():
    assert "*" not in trusted_ranges()
    assert client_seen("172.18.0.3") == "198.51.100.7"  # Caddy on the Docker network
    assert client_seen("192.168.65.1") == "198.51.100.7"
    assert client_seen("203.0.113.9") == "203.0.113.9"  # anyone else can't choose their address
