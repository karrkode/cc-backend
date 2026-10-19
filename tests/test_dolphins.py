"""CC-61: the plan dolphins are a per-account setting, off unless turned on with scripts.dolphins."""
import sys

import pytest

from scripts import dolphins

from .conftest import auth, register


def me(client, token):
    return client.get("/auth/me", headers=auth(token)).json()


def run(monkeypatch, Session, *args):
    monkeypatch.setattr(dolphins, "SessionLocal", Session)
    monkeypatch.setattr(sys, "argv", ["dolphins", *args])
    dolphins.main()


def test_off_by_default_and_the_script_turns_it_on_and_off(client, monkeypatch, Session, capsys):
    token = register(client, email="Owner@Example.com")
    other = register(client, email="someone@example.com", name="Bo")
    assert me(client, token)["dolphins"] is False

    run(monkeypatch, Session, "owner@example.com")
    assert capsys.readouterr().out == "owner@example.com: dolphins on.\n"
    assert me(client, token)["dolphins"] is True
    assert me(client, other)["dolphins"] is False  # only the account it was turned on for

    run(monkeypatch, Session, "OWNER@example.com", "--off")
    assert me(client, token)["dolphins"] is False


def test_unknown_account(monkeypatch, Session):
    with pytest.raises(SystemExit, match="No account for nobody@example.com"):
        run(monkeypatch, Session, "nobody@example.com")


def test_sign_in_reports_the_setting(client, monkeypatch, Session):
    register(client, email="owner@example.com", password="correct-horse-1")
    run(monkeypatch, Session, "owner@example.com")
    r = client.post("/auth/login", json={"email": "owner@example.com", "password": "correct-horse-1"})
    assert r.json()["user"]["dolphins"] is True
