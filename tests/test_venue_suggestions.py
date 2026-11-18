"""CC-209: members suggest a venue's calendar link; admins accept or decline."""
import pytest
from sqlalchemy import select

from app import venuesuggestions as vs
from app.config import get_settings
from app.discovery import fetch as web
from app.models import DiscoverySource, SourceKind, VenueSuggestion

from .conftest import auth, register

URL = "https://venue.example/calendar"


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch):
    monkeypatch.setattr(web, "_addresses", lambda host, port: ["93.184.215.14"])
    monkeypatch.setattr(web, "fetch", lambda *a, **k: pytest.fail("a suggestion must not fetch anything"))


def suggest(client, token, url=URL, **extra):
    return client.post("/venue-suggestions", json={"url": url, **extra}, headers=auth(token))


def test_submit_saves_it_and_the_member_sees_it(client, user_token):
    r = suggest(client, user_token, venue_name="Venue", note="Great stage")
    assert r.status_code == 201 and r.json()["status"] == "pending" and r.json()["venue_name"] == "Venue"
    mine = client.get("/venue-suggestions/mine", headers=auth(user_token)).json()
    assert [m["url"] for m in mine] == [URL]
    assert "suggested_by" not in mine[0]


def test_signed_in_only(client):
    assert client.post("/venue-suggestions", json={"url": URL}).status_code == 401
    assert client.get("/venue-suggestions/mine").status_code == 401


def test_unconfirmed_email_is_refused_and_uses_no_limit(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "require_verified_email", True)
    t = register(client)
    r = suggest(client, t)
    assert r.status_code == 403 and r.json()["detail"].startswith("Confirm your email address before")


@pytest.mark.parametrize("url,addr", [
    ("http://localhost/x", "127.0.0.1"), ("http://10.0.0.5/", "10.0.0.5"), ("http://169.254.169.254/latest/", "169.254.169.254"),
    ("ftp://files.example/x", "93.184.215.14"), ("https://u:pw@site.example/", "93.184.215.14"), ("https://site.example:8443/", "93.184.215.14")])
def test_private_and_odd_links_are_refused(client, user_token, monkeypatch, url, addr):
    monkeypatch.setattr(web, "_addresses", lambda host, port: [addr])
    r = suggest(client, user_token, url)
    assert r.status_code == 422 and r.json()["detail"].startswith("We can't use that link")


def test_five_a_day(client, user_token, db, monkeypatch):
    monkeypatch.setattr(vs, "PER_MINUTE", 50)
    codes = [suggest(client, user_token, f"https://v{i}.example/cal").status_code for i in range(vs.PER_DAY + 1)]
    assert codes == [201] * vs.PER_DAY + [429]
    other = register(client, email="bo@example.com", name="Bo")
    assert suggest(client, other, "https://other.example/cal").status_code == 201  # per member


def test_a_link_already_read_or_waiting_is_refused_alike(client, user_token, admin_token, db):
    db.add(DiscoverySource(name="Known", kind=SourceKind.theater, pages=["https://known.example/events/"]))
    db.commit()
    a = suggest(client, user_token, "https://www.known.example/events")
    assert a.status_code == 409 and a.json()["detail"] == vs.ALREADY
    assert suggest(client, user_token).status_code == 201
    b = suggest(client, admin_token, URL + "#top")
    assert b.status_code == 409 and b.json()["detail"] == vs.ALREADY


def test_admin_list_shows_who(client, user_token, admin_token):
    suggest(client, user_token)
    assert client.get("/admin/venue-suggestions", headers=auth(user_token)).status_code == 403
    rows = client.get("/admin/venue-suggestions", headers=auth(admin_token)).json()
    assert [r["suggested_by"] for r in rows] == ["Ana"]


def test_accept_adds_a_source(client, user_token, admin_token, db):
    sid = suggest(client, user_token, venue_name="Venue Hall").json()["id"]
    assert client.post(f"/admin/venue-suggestions/{sid}/accept", json={}, headers=auth(user_token)).status_code == 403
    r = client.post(f"/admin/venue-suggestions/{sid}/accept", json={"kind": "music"}, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    src = db.scalar(select(DiscoverySource).where(DiscoverySource.name == "Venue Hall"))
    assert src.pages == [URL] and src.kind == SourceKind.music and src.active and src.next_check_on is not None
    assert r.json()["source_id"] == src.id
    assert client.post(f"/admin/venue-suggestions/{sid}/accept", json={}, headers=auth(admin_token)).status_code == 409
    assert client.get("/venue-suggestions/mine", headers=auth(user_token)).json()[0]["status"] == "accepted"


def test_accept_without_a_name_uses_the_host_and_rejects_a_taken_name(client, user_token, admin_token, db):
    a = suggest(client, user_token).json()["id"]
    assert client.post(f"/admin/venue-suggestions/{a}/accept", json={}, headers=auth(admin_token)).status_code == 200
    assert db.scalar(select(DiscoverySource).where(DiscoverySource.name == "venue.example"))
    b = suggest(client, user_token, "https://venue.example/other").json()["id"]
    assert client.post(f"/admin/venue-suggestions/{b}/accept", json={}, headers=auth(admin_token)).status_code == 409
    assert db.get(VenueSuggestion, b).status == "pending"


def test_accept_checks_the_address_again(client, user_token, admin_token, db, monkeypatch):
    sid = suggest(client, user_token).json()["id"]
    monkeypatch.setattr(web, "_addresses", lambda host, port: ["10.0.0.5"])
    assert client.post(f"/admin/venue-suggestions/{sid}/accept", json={}, headers=auth(admin_token)).status_code == 422
    assert db.scalars(select(DiscoverySource)).first() is None


def test_decline_keeps_the_reason_for_the_member(client, user_token, admin_token, db):
    sid = suggest(client, user_token).json()["id"]
    r = client.post(f"/admin/venue-suggestions/{sid}/decline", json={"reason": "Not a venue"}, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["status"] == "declined"
    mine = client.get("/venue-suggestions/mine", headers=auth(user_token)).json()[0]
    assert mine["status"] == "declined" and mine["decline_reason"] == "Not a venue"
    assert client.post(f"/admin/venue-suggestions/{sid}/decline", json={}, headers=auth(admin_token)).status_code == 409
    assert db.scalars(select(DiscoverySource)).first() is None
    assert suggest(client, user_token).status_code == 201  # a declined link can be suggested again


def test_deleting_the_member_keeps_the_suggestion(client, user_token, admin_token, db):
    from app.models import User
    suggest(client, user_token)
    db.delete(db.scalar(select(User).where(User.email == "ana@example.com")))
    db.commit()
    assert client.get("/admin/venue-suggestions", headers=auth(admin_token)).json()[0]["suggested_by"] == ""


def test_a_host_that_does_not_exist_is_a_422_not_a_500(client, user_token, admin_token, monkeypatch):
    def none(host, port):
        raise web.FetchError("no such host")
    sid = suggest(client, user_token).json()["id"]
    monkeypatch.setattr(web, "_addresses", none)
    r = suggest(client, user_token, "https://gone.example/cal")
    assert r.status_code == 422 and r.json()["detail"].startswith("We can't use that link")
    assert client.post(f"/admin/venue-suggestions/{sid}/accept", json={}, headers=auth(admin_token)).status_code == 422


def test_every_attempt_counts_toward_a_minute_limit(client, user_token, monkeypatch):
    monkeypatch.setattr(web, "_addresses", lambda host, port: ["10.0.0.5"])
    codes = [suggest(client, user_token, "http://10.0.0.5/x").status_code for _ in range(vs.PER_MINUTE + 1)]
    assert codes[-1] == 429 and set(codes[:-1]) == {422}


def test_the_admins_name_becomes_the_sources_venue_name(client, user_token, admin_token, db):
    sid = suggest(client, user_token, venue_name="Joes bar lol").json()["id"]
    client.post(f"/admin/venue-suggestions/{sid}/accept", json={"name": "  Joe's Bar "}, headers=auth(admin_token))
    src = db.scalar(select(DiscoverySource))
    assert (src.name, src.venue_name) == ("Joe's Bar", "Joe's Bar")


def test_accept_refuses_a_page_a_source_now_reads(client, user_token, admin_token, db):
    sid = suggest(client, user_token).json()["id"]
    db.add(DiscoverySource(name="Added since", kind=SourceKind.theater, pages=[URL]))
    db.commit()
    assert client.post(f"/admin/venue-suggestions/{sid}/accept", json={}, headers=auth(admin_token)).status_code == 409


def test_the_list_puts_the_most_suggested_site_first(client, user_token, admin_token, db):
    # CC-210: two members on one site outrank one member on another, though the other came first
    other = register(client, email="bo@example.com", name="Bo")
    a = suggest(client, user_token, "https://solo.example/events").json()["id"]
    b = suggest(client, user_token, "https://pair.example/calendar").json()["id"]
    c = suggest(client, other, "https://www.pair.example/shows").json()["id"]
    rows = client.get("/admin/venue-suggestions", headers=auth(admin_token)).json()
    assert [r["id"] for r in rows] == [b, c, a]
    assert [r["same_site"] for r in rows] == [2, 2, 1]
    # the same member twice on one site still counts once, and decided ones leave the count
    d = suggest(client, user_token, "https://solo.example/other").json()["id"]
    assert {r["id"]: r["same_site"] for r in client.get("/admin/venue-suggestions", headers=auth(admin_token)).json()}[a] == 1
    client.post(f"/admin/venue-suggestions/{b}/decline", json={}, headers=auth(admin_token))
    rows = client.get("/admin/venue-suggestions", headers=auth(admin_token)).json()
    assert {r["id"]: r["same_site"] for r in rows}[c] == 1
    assert d in [r["id"] for r in rows]
