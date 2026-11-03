"""CC-186: with require_verified_email on (production), sharing a take, sending a photo, reporting an issue and reading
from a link need a confirmed address, as sending in an event does (CC-46). Admins aren't held up."""
import pytest

from app.config import get_settings
from app.routers import issues as issues_router

from .conftest import auth, register
from .test_email_flows import link_token
from .test_from_link import LD_PAGE, ask, claude, web_pages  # noqa: F401 (fixtures)
from .test_issues import REPORT
from .test_journal import been, journal
from .test_takes import share
from .test_went_photos import stored, upload  # noqa: F401 (fixture)


@pytest.fixture
def unconfirmed(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "require_verified_email", True)
    issues_router.report_limiter.reset()
    return register(client)


def confirm(client):
    assert client.post("/auth/verify-email", json={"token": link_token("verify", to="ana@example.com")}).status_code == 200


def refused(r):
    return r.status_code == 403 and r.json()["detail"].startswith("Confirm your email address before")


def test_sharing_a_take(client, unconfirmed, make_event):
    h, ev = auth(unconfirmed), make_event()
    been(client, h, ev)
    journal(client, h, ev, words="moody dark grand", review="Loved it.", recommend=True)
    assert refused(share(client, h, ev))
    assert client.get(f"/e/{ev.id}").text.count("Loved it.") == 0
    assert client.put(f"/me/attended/{ev.id}/share", json={"shared": False}, headers=h).status_code == 200  # stopping is fine
    confirm(client)
    assert share(client, h, ev).status_code == 200
    assert "Loved it." in client.get(f"/e/{ev.id}").text


def test_sending_a_photo(client, unconfirmed, make_event, stored):  # noqa: F811
    h, ev = auth(unconfirmed), make_event()
    been(client, h, ev)
    assert refused(upload(client, unconfirmed, ev.id))
    confirm(client)
    assert upload(client, unconfirmed, ev.id).status_code == 200


def test_reporting_an_issue(client, unconfirmed):
    h = auth(unconfirmed)
    assert refused(client.post("/issues", json=REPORT, headers=h))
    assert client.get("/me/issues", headers=h).json() == []
    confirm(client)
    assert client.post("/issues", json=REPORT, headers=h).status_code == 201


def test_reading_from_a_link(client, unconfirmed, web_pages, claude):  # noqa: F811
    web_pages["https://theatre.example/seagull"] = LD_PAGE
    assert refused(ask(client, unconfirmed, "https://theatre.example/seagull"))
    confirm(client)
    assert ask(client, unconfirmed, "https://theatre.example/seagull").status_code == 200


def test_admins_and_local_runs_arent_held_up(client, admin_token, make_event, monkeypatch):
    issues_router.report_limiter.reset()
    monkeypatch.setattr(get_settings(), "require_verified_email", True)
    assert client.post("/issues", json=REPORT, headers=auth(admin_token)).status_code == 201  # never confirmed
    monkeypatch.setattr(get_settings(), "require_verified_email", False)
    h, ev = auth(register(client, email="bo@example.com", name="Bo")), make_event()
    been(client, h, ev)
    journal(client, h, ev, words="one two three")
    assert share(client, h, ev).status_code == 200
