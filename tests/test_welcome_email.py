"""CC-243: the welcome email, sent once, the first time an account's address is confirmed."""
import re

from sqlalchemy import select

from app import mail
from app.mail import OUTBOX, first_name
from app.models import User

from .conftest import auth, register

SUBJECT = "So what is ChiCultureCrew... (an explainer)"


def welcomes():
    return [e for e in OUTBOX if e.subject == SUBJECT]


def confirm(client, to="ana@example.com"):
    email = [e for e in OUTBOX if e.to == to and "#verify=" in e.text][-1]
    raw = re.search(r"/#verify=([\w-]+)", email.text).group(1)
    return client.post("/auth/verify-email", json={"token": raw})


def test_confirm_email_wording():
    email = mail.verify_email(User(email="ana@example.com", password_hash="x", display_name="Ana"), "tok")
    assert email.subject == "ChiCultureCrew needs you to validate your email!"
    assert "\n\nhttp://localhost:4173/#verify=tok\n\n" in email.text  # on its own line, so no punctuation joins the link
    assert "expires in 24 hours" in email.text and "you'll get a walk-through of the site" in email.text
    assert email.text.endswith("Your cultural collaborator,\nAndrew Carr\nhttp://localhost:4173\n")
    assert email.html == ""


def test_confirming_sends_the_welcome_once(client):
    h = auth(register(client, name="Ana Cruz"))
    assert welcomes() == []  # not before confirming
    assert confirm(client).status_code == 200
    [email] = welcomes()
    assert email.to == "ana@example.com"
    assert email.text.startswith("Ana!\n\nYou've confirmed your email, excellent!")
    for words in ("Choose, Commit, Communicate", "Interested button", "Upgrade that Interested to Going!",
                  "it shows up in My week", "Report an issue or request a feature",
                  "Your cultural collaborator,\nAndrew Carr\nhttp://localhost:4173\n"):
        assert words in email.text
    assert email.html == ""

    assert confirm(client).status_code == 200  # the same link again (a mail scanner): no second welcome
    assert len(welcomes()) == 1

    # a new address must be confirmed again, but the account has already been welcomed
    r = client.patch("/auth/me", json={"display_name": "Ana Cruz", "email": "ana2@example.com",
                                       "current_password": "correct-horse-1"}, headers=h)
    assert r.status_code == 200 and r.json()["email_verified"] is False
    assert client.post("/auth/verify-email/send", headers=h).status_code == 200
    assert confirm(client, to="ana2@example.com").status_code == 200
    assert len(welcomes()) == 1


def test_a_password_reset_confirms_and_welcomes(client, db):
    register(client, name="Ana")
    assert client.post("/auth/forgot-password", json={"email": "ana@example.com"}).status_code == 202
    raw = re.search(r"/#reset=([\w-]+)", OUTBOX[-1].text).group(1)
    assert client.post("/auth/reset-password", json={"token": raw, "new_password": "battery-staple-2"}).status_code == 200
    assert [e.to for e in welcomes()] == ["ana@example.com"]
    assert db.scalar(select(User).where(User.email == "ana@example.com")).welcomed_at is not None


def test_the_greeting_is_a_cleaned_capped_first_name():
    def greeting(name):
        return mail.welcome(User(email="a@example.com", password_hash="x", display_name=name)).text.split("\n")[0]
    assert greeting("Ana Cruz") == "Ana!"
    assert greeting("Ana\nClick https://evil.example") == "Ana!"
    assert greeting("https://evil.example Ana") == "Ana!"  # a link-like first word is dropped, not shown
    assert greeting("www.evil.example") == "Hello hello!"
    assert greeting("") == "Hello hello!"
    assert greeting("B" * 80) == f"{'B' * 19}…!"
    assert first_name("  Mal\tlory ") == "Mal"


def test_the_subject_never_carries_the_name():
    email = mail.welcome(User(email="a@example.com", password_hash="x", display_name="Visit evil now"))
    assert email.subject == SUBJECT and "Visit" in email.text.split("\n")[0]
