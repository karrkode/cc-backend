"""CC-40: people edit their own name and email (PATCH /auth/me)."""
from sqlalchemy import select

from app.models import User

from .conftest import auth, register

PW = "correct-horse-1"  # register()'s default password


def patch(client, token, **body):
    return client.patch("/auth/me", headers=auth(token), json=body)


def login(client, email, password=PW):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_change_name_without_a_password(client, user_token):
    r = patch(client, user_token, display_name="  Ana Lucía  ")
    assert r.status_code == 200 and r.json()["display_name"] == "Ana Lucía"  # trimmed
    assert client.get("/auth/me", headers=auth(user_token)).json()["display_name"] == "Ana Lucía"


def test_blank_name_is_allowed(client, user_token):
    assert patch(client, user_token, display_name="   ").json()["display_name"] == ""


def test_change_email_needs_the_current_password(client, user_token):
    assert patch(client, user_token, email="new@example.com").status_code == 403
    assert patch(client, user_token, email="new@example.com", current_password="wrong-password").status_code == 403
    r = patch(client, user_token, email="New@Example.com", current_password=PW)
    assert r.status_code == 200 and r.json()["email"] == "new@example.com"  # stored lower-case
    assert login(client, "new@example.com").status_code == 200
    assert login(client, "ana@example.com").status_code == 401


def test_same_email_in_other_case_is_not_a_change(client, user_token):
    r = patch(client, user_token, email="ANA@example.com", display_name="Ana B")  # no password needed
    assert r.status_code == 200 and r.json()["email"] == "ana@example.com"


def test_email_taken_by_someone_else_saves_nothing(client, user_token):
    register(client, email="taken@example.com", name="Other")
    r = patch(client, user_token, email="Taken@example.com", display_name="Renamed", current_password=PW)
    assert r.status_code == 409
    me = client.get("/auth/me", headers=auth(user_token)).json()
    assert me["email"] == "ana@example.com" and me["display_name"] == "Ana"  # all or nothing


def test_role_and_id_cannot_be_changed(client, user_token, db):
    before = client.get("/auth/me", headers=auth(user_token)).json()
    r = patch(client, user_token, display_name="Ana", role="admin", id="00000000-0000-0000-0000-000000000000")
    assert r.status_code == 200 and r.json()["role"] == "user" and r.json()["id"] == before["id"]
    assert db.scalar(select(User).where(User.email == "ana@example.com")).role.value == "user"


def test_session_survives_an_email_change(client, user_token):
    patch(client, user_token, email="moved@example.com", current_password=PW)
    assert client.get("/auth/me", headers=auth(user_token)).status_code == 200  # same access token
    assert client.post("/auth/refresh").status_code == 200  # same refresh cookie


def test_validation_and_sign_in(client, user_token):
    assert client.patch("/auth/me", json={"display_name": "X"}).status_code == 401
    assert patch(client, user_token, email="not-an-email", current_password=PW).status_code == 422
    assert patch(client, user_token, display_name="x" * 81).status_code == 422
    assert patch(client, user_token).status_code == 200  # nothing to change is fine


def test_new_name_shows_on_credited_events(client, user_token, admin_token):
    ev = client.post("/events", headers=auth(user_token), json={
        "title": "Credit me", "category": "Music", "venue": {"name": "Annoyance", "neighborhood": "Lakeview"},
        "credit_me": True}).json()
    client.post(f"/admin/events/{ev['id']}/approve", headers=auth(admin_token))
    patch(client, user_token, display_name="Ana Lucía")
    assert client.get(f"/events/{ev['id']}").json()["submitted_by_name"] == "Ana Lucía"
    patch(client, user_token, display_name="")
    assert client.get(f"/events/{ev['id']}").json()["submitted_by_name"] is None


def test_profile_edits_are_rate_limited(client, user_token):
    codes = [patch(client, user_token, email="x@example.com", current_password="guess-" + str(i)).status_code
             for i in range(11)]
    assert codes[:10] == [403] * 10 and codes[10] == 429
