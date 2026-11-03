"""CC-213: venues have an admin-only website. Admins confirm suggested ones; nothing is filled in by itself."""
import pytest

from app.main import app
from app.models import DiscoverySource, SourceKind, Venue, VenueWebsiteDismissal
from app.websites import normalize_website

from .conftest import auth

SITE = "https://hall-website.example/"


@pytest.mark.parametrize("raw, stored", [
    ("https://Example.ORG", "https://example.org/"),
    ("  HTTP://www.example.org:80/whats-on?x=1#top ", "http://www.example.org/whats-on?x=1"),
    ("https://example.org:443/a", "https://example.org/a"),
    ("https://example.org:8443/", "https://example.org:8443/"),
    ("", ""),
])
def test_websites_are_normalised(raw, stored):
    assert normalize_website(raw) == stored


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "data:text/html,<b>x</b>", "ftp://example.org/", "example.org", "//example.org/",
    "https://", "https://localhost/", "https://user:pw@example.org/", "https://exa mple.org/",
    "https://example.org/\nx", 'https://x.example/"><svg/onload=alert(1)>', "https://x.example/'onmouseover=1", "https://example.org:99999/", "https://" + "a" * 600 + ".org/",
])
def test_only_http_addresses_are_accepted(client, admin_token, make_event, bad):
    venue_id = make_event().venue_id
    r = client.patch(f"/venues/{venue_id}", json={"website": bad}, headers=auth(admin_token))
    assert r.status_code == 422
    assert client.post(f"/venues/{venue_id}/website", json={"url": bad}, headers=auth(admin_token)).status_code == 422


def test_admins_set_edit_and_clear_a_website(client, admin_token, user_token, make_event):
    venue_id = make_event().venue_id
    h = auth(admin_token)
    r = client.patch(f"/venues/{venue_id}", json={"website": "HTTPS://Hall-Website.example"}, headers=h)
    assert r.status_code == 200 and r.json()["website"] == SITE
    assert [v["website"] for v in client.get("/venues/manage", headers=h).json()] == [SITE]
    assert client.patch(f"/venues/{venue_id}", json={"name": "Renamed"}, headers=h).json()["website"] == SITE  # left alone
    assert client.patch(f"/venues/{venue_id}", json={"website": ""}, headers=h).json()["website"] == ""
    assert client.patch(f"/venues/{venue_id}", json={"website": SITE}, headers=auth(user_token)).status_code == 403


def _get_routes():
    paths = app.openapi()["paths"]
    yield from (path for path, ops in paths.items() if "get" in ops)
    yield "/chicago/"  # left out of the schema


def test_non_admins_never_receive_the_website(client, admin_token, user_token, db, make_event):
    ev = make_event()
    client.patch(f"/venues/{ev.venue_id}", json={"website": SITE}, headers=auth(admin_token))
    db.add(DiscoverySource(name="Hall", kind=SourceKind.theater, pages=["https://other-hall.example/cal"],
                           reader="claude", venue_id=ev.venue_id))
    db.commit()
    params = {"event_id": ev.id, "ref": ev.id, "venue_id": ev.venue_id}
    checked = 0
    for path in _get_routes():
        try:
            url = path.format(**params)
        except KeyError:  # a route for something this test doesn't make (a crew, a photo...)
            continue
        for headers in ({}, auth(user_token)):
            r = client.get(url, headers=headers)
            assert "hall-website" not in r.text and '"website":' not in r.text, (url, headers)
            checked += r.status_code == 200
    assert checked > 10
    for url in ("/venues/manage", "/venues/website-suggestions"):
        assert client.get(url, headers=auth(user_token)).status_code == 403
        assert client.get(url).status_code == 401
    assert client.post(f"/venues/{ev.venue_id}/website", json={"url": SITE}, headers=auth(user_token)).status_code == 403
    assert client.post(f"/venues/{ev.venue_id}/website/dismiss", json={"url": SITE},
                       headers=auth(user_token)).status_code == 403


def _suggested(client, h):
    return [(s["venue_name"], s["url"], s["source"]) for s in client.get("/venues/website-suggestions", headers=h).json()]


def test_suggestions_come_from_discovery_sources_and_the_cc205_report(client, admin_token, db, make_event):
    hall = make_event(venue_name="Hall").venue_id
    make_event("Macbeth", venue_name="Chicago Shakespeare Theater", neighborhood="Streeterville")
    make_event("Lear", venue_name="Nowhere Club")
    museum = make_event("Monet", venue_name="Art Museum").venue_id
    db.add_all([
        DiscoverySource(name="Hall", kind=SourceKind.theater, pages=["https://WWW.Hall.example/whats-on/calendar"],
                        reader="claude", venue_id=hall),
        DiscoverySource(name="Hall again", kind=SourceKind.theater, pages=["https://www.hall.example/other"],
                        reader="claude", venue_id=hall),
        DiscoverySource(name="Museum API", kind=SourceKind.museum, pages=["https://api.museum.example/v1"],
                        reader="aic_api", venue_id=museum),  # an API, not the museum's site
        DiscoverySource(name="Unlinked", kind=SourceKind.theater, pages=["https://unlinked.example/"],
                        reader="claude", venue_name="Somewhere"),
    ])
    db.commit()
    assert _suggested(client, auth(admin_token)) == [
        ("Chicago Shakespeare Theater", "https://www.chicagoshakes.com/", "cc205"),
        ("Hall", "https://www.hall.example/", "discovery"),
    ]
    assert db.get(Venue, hall).website == ""  # suggesting saves nothing


def test_confirm_saves_and_dismiss_is_remembered(client, admin_token, db, make_event):
    shakes = make_event("Macbeth", venue_name="Chicago Shakespeare Theater", neighborhood="Streeterville").venue_id
    looking = make_event("Pericles", venue_name="Lookingglass Theatre", neighborhood="Streeterville").venue_id
    h = auth(admin_token)
    assert len(_suggested(client, h)) == 2

    r = client.post(f"/venues/{shakes}/website", json={"url": "https://www.chicagoshakes.com/"}, headers=h)
    assert r.status_code == 200 and r.json()["website"] == "https://www.chicagoshakes.com/"
    assert _suggested(client, h) == [("Lookingglass Theatre", "https://lookingglasstheatre.org/", "cc205")]

    assert client.post(f"/venues/{looking}/website/dismiss", json={"url": "https://lookingglasstheatre.org"},
                       headers=h).status_code == 204
    assert client.post(f"/venues/{looking}/website/dismiss", json={"url": "https://lookingglasstheatre.org/"},
                       headers=h).status_code == 204  # twice is fine
    assert _suggested(client, h) == [] and db.get(Venue, looking).website == ""
    assert db.query(VenueWebsiteDismissal).count() == 1

    # an edited suggestion is confirmed as edited
    r = client.post(f"/venues/{looking}/website", json={"url": "https://Lookingglasstheatre.org/visit"}, headers=h)
    assert r.json()["website"] == "https://lookingglasstheatre.org/visit"


def test_a_suggestion_never_overwrites_an_admins_website(client, admin_token, db, make_event):
    shakes = make_event("Macbeth", venue_name="Chicago Shakespeare Theater", neighborhood="Streeterville").venue_id
    h = auth(admin_token)
    client.patch(f"/venues/{shakes}", json={"website": "https://admin-choice.example/"}, headers=h)
    assert _suggested(client, h) == []  # a venue with a website gets no suggestions
    r = client.post(f"/venues/{shakes}/website", json={"url": "https://www.chicagoshakes.com/"}, headers=h)
    assert r.status_code == 409
    assert db.get(Venue, shakes).website == "https://admin-choice.example/"


def test_merging_keeps_a_website(client, admin_token, db, make_event):
    a = make_event("A", venue_name="Hall", neighborhood="Loop").venue_id
    b = make_event("B", venue_name="Hall Two", neighborhood="Loop").venue_id
    h = auth(admin_token)
    client.patch(f"/venues/{a}", json={"website": SITE}, headers=h)
    client.post(f"/venues/{a}/website/dismiss", json={"url": "https://x.example/"}, headers=h)
    assert client.post(f"/venues/{a}/merge-into/{b}", headers=h).status_code == 200
    assert db.get(Venue, b).website == SITE
    assert db.query(VenueWebsiteDismissal).count() == 0  # went with the merged venue
