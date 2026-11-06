"""CC-222: adding a venue by its website: the page's structured data first, Claude only for what's still missing."""
import socket
from types import SimpleNamespace

import pytest

from app import fromlink, venuelink
from app.discovery import fetch as web
from app.discovery.budget import budget_state, set_monthly_budget
from app.discovery.fetch import Page, Robots
from app.discovery.jsonld import venue_in
from app.discovery.pagetext import MAX_CHARS, page_text
from app.discovery.readers import VenueReading
from app.discovery.venuesources import add_missing
from app.models import DiscoveryCheck, DiscoverySource, Venue
from app.stats import today_in_chicago

from .conftest import auth

SITE = "https://lakeshore.example/"
ORG = """<script type="application/ld+json">{"@context": "https://schema.org", "@type": "PerformingArtsTheater",
"name": "Lakeshore Playhouse", "address": {"@type": "PostalAddress", "streetAddress": "1650 N Halsted St",
"addressLocality": "Chicago", "addressRegion": "IL", "postalCode": "60614"}}</script>"""
PLAIN = "<html><body><h1>Welcome</h1><footer>Lakeshore Playhouse, 1650 N Halsted St, Chicago 60614</footer></body></html>"


def ld(body: str) -> str:
    return f'<script type="application/ld+json">{body}</script>'


class FakeClaude:
    def __init__(self, **said):
        self.said, self.calls, self.sent = said, 0, ""
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls += 1
        self.sent = kw["messages"][0]["content"]
        assert kw["model"] == "claude-haiku-5-5" and "street" in kw["output_config"]["format"]["schema"]["properties"]
        return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=1000, output_tokens=50),
                               content=[SimpleNamespace(type="text", text=VenueReading(**self.said).model_dump_json())])


@pytest.fixture
def web_pages(monkeypatch):
    served = {}

    def fake_fetch(url, etag="", last_modified="", ai=True, timeout=25, **limits):
        web.check_public(url)
        got = served[url]
        if isinstance(got, Exception):
            raise got
        return Page(url=url, status=200, text=got)
    monkeypatch.setattr(web, "fetch", fake_fetch)
    monkeypatch.setattr(web, "robots_for", lambda url, timeout=25: Robots([], [], 0.0, served.get("ai_opt_out", False)))
    monkeypatch.setattr(web, "_addresses", lambda host, port: ["93.184.215.14"])
    monkeypatch.setattr(fromlink, "bounded_read", lambda url, work: work(url))  # in this process, so the fakes apply
    return served


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude(name="Lakeshore Playhouse", street="1650 N Halsted St", city="Chicago", state="IL", zip_code="60614",
                      neighborhood="Lincoln Park")
    monkeypatch.setattr(fromlink, "claude_client", lambda: fake)
    return fake


def ask(client, token, url=SITE):
    return client.post("/venues/from-website", json={"url": url}, headers=auth(token))


def add(client, token, **body):
    place = {"name": "Lakeshore Playhouse", "address": "1650 N Halsted St", "city": "Chicago", "zip_code": "60614"}
    return client.post("/venues", json={**place, **body}, headers=auth(token))


def test_structured_data_fills_the_form_without_claude(client, admin_token, db, web_pages, claude):
    web_pages[SITE] = ORG
    got = ask(client, admin_token).json()
    assert (got["name"], got["address"], got["city"], got["zip_code"]) == ("Lakeshore Playhouse", "1650 N Halsted St", "Chicago", "60614")
    assert got["missing"] == [] and got["problem"] == "" and got["website"] == SITE
    assert got["neighborhood"] == "", "Chicago doesn't say which neighborhood"
    assert claude.calls == 0
    assert db.query(Venue).count() == 0, "nothing is saved before Add venue"
    assert db.query(DiscoveryCheck).one().input_tokens == 0


def test_claude_reads_a_page_without_them_and_it_counts_toward_the_budget(client, admin_token, db, web_pages, claude):
    web_pages[SITE] = PLAIN
    before = budget_state(db)[1]
    got = ask(client, admin_token).json()
    assert got["name"] == "Lakeshore Playhouse" and got["zip_code"] == "60614" and got["neighborhood"] == "Lincoln Park"
    assert claude.calls == 1 and "1650 N Halsted St" in claude.sent, "the footer, where sites give their address, reaches Claude"
    check = db.query(DiscoveryCheck).one()
    assert (check.what, check.input_tokens, check.output_tokens) == ("link", 1000, 50)
    assert "venue website" in check.note and budget_state(db)[1] > before


def test_claude_only_adds_to_what_the_page_gave(client, admin_token, web_pages, claude):
    web_pages[SITE] = ld('{"@type": "Organization", "name": "Lakeshore Theatre Company"}') + PLAIN
    got = ask(client, admin_token).json()
    assert claude.calls == 1 and got["name"] == "Lakeshore Theatre Company" and got["address"] == "1650 N Halsted St"


@pytest.mark.parametrize("stop", ["budget", "opt_out", "no_key"])
def test_no_claude_read_and_the_form_is_told_what_is_missing(client, admin_token, db, web_pages, claude, monkeypatch, stop):
    web_pages[SITE] = ld('{"@type": "Museum", "name": "Lakeshore Museum"}') + PLAIN
    if stop == "budget":
        db.add(DiscoveryCheck(source_id=None, what="link", status="ok", input_tokens=10**9, output_tokens=0)); db.commit()
        set_monthly_budget(db, 1)
    elif stop == "opt_out":
        web_pages["ai_opt_out"] = True
    else:
        monkeypatch.setattr(fromlink, "claude_client", lambda: None)
    got = ask(client, admin_token).json()
    assert claude.calls == 0
    assert got["name"] == "Lakeshore Museum" and got["missing"] == ["address", "city", "zip_code"] and got["problem"] == ""


def test_a_claude_failure_leaves_what_the_page_gave(client, admin_token, db, web_pages, claude, monkeypatch):
    def fails(**kw):
        raise ValueError("no")
    claude.messages.create = fails
    web_pages[SITE] = ld('{"@type": "Museum", "name": "Lakeshore Museum"}') + PLAIN
    got = ask(client, admin_token)
    assert got.status_code == 200 and got.json()["name"] == "Lakeshore Museum"
    assert db.query(DiscoveryCheck).one().status == "error"


def test_a_failed_claude_read_counts_its_tokens(client, admin_token, db, web_pages, claude):
    """CC-198"""
    claude.messages.create = lambda **kw: SimpleNamespace(stop_reason="refusal", content=[],
                                                          usage=SimpleNamespace(input_tokens=700, output_tokens=0))
    web_pages[SITE] = ld('{"@type": "Museum", "name": "Lakeshore Museum"}') + PLAIN
    assert ask(client, admin_token).json()["name"] == "Lakeshore Museum"
    check = db.query(DiscoveryCheck).one()
    assert (check.status, check.input_tokens, check.pages_read) == ("error", 700, 1)


@pytest.mark.parametrize("failure, says", [(web.Blocked("no"), "doesn't let us"), (web.FetchError("404"), "couldn't open")])
def test_a_page_that_cannot_be_read_says_so(client, admin_token, web_pages, claude, failure, says):
    web_pages[SITE] = failure
    res = ask(client, admin_token)
    assert res.status_code == 200 and says in res.json()["problem"]
    assert res.json()["missing"] == ["name", "address", "city", "zip_code"] and claude.calls == 0


def test_a_private_address_is_refused_before_any_read(client, admin_token, db, web_pages, claude, monkeypatch):
    monkeypatch.setattr(web, "_addresses", lambda host, port: ["10.0.0.5"])
    res = ask(client, admin_token, "https://intranet.example/")
    assert res.status_code == 200 and "can't read that link" in res.json()["problem"]
    res = ask(client, admin_token, "http://169.254.169.254/latest/")
    assert "can't read that link" in res.json()["problem"]


def test_what_is_not_a_web_address_is_refused(client, admin_token, web_pages):
    assert ask(client, admin_token, "ftp://lakeshore.example/").status_code == 422
    assert ask(client, admin_token, "lakeshore").status_code == 422


def test_a_host_that_does_not_exist_says_so(client, admin_token, web_pages, monkeypatch):
    def no_such_host(host, port):
        raise socket.gaierror("no")
    monkeypatch.setattr(web, "_addresses", no_such_host)
    res = ask(client, admin_token)
    assert res.status_code == 200 and "couldn't open" in res.json()["problem"]


def test_a_website_another_venue_has_is_named_even_if_the_page_cannot_be_read(client, admin_token, db, web_pages, claude):
    db.add(Venue(name="Old Playhouse", neighborhood="Uptown", website="http://www.lakeshore.example"))
    db.add(Venue(name="Elsewhere", neighborhood="Loop", website="https://elsewhere.example/"))
    db.commit()
    web_pages[SITE] = ORG
    assert ask(client, admin_token).json()["same_website"] == [{"name": "Old Playhouse", "neighborhood": "Uptown"}]
    web_pages[SITE] = web.FetchError("gone")
    got = ask(client, admin_token).json()
    assert got["problem"] and got["same_website"][0]["name"] == "Old Playhouse"
    assert add(client, admin_token, website=SITE).status_code == 201, "the admin can still go on"


def test_add_venue_saves_the_website_and_discovery_adds_its_source(client, admin_token, db):
    res = add(client, admin_token, website="HTTPS://Lakeshore.example")
    assert res.status_code == 201 and "website" not in res.json(), "still admin-only"
    venue = db.get(Venue, res.json()["id"])
    assert venue.website == SITE
    assert add_missing(db, today_in_chicago()) == 1
    assert db.query(DiscoverySource).one().pages == [SITE]


def test_add_venue_without_a_website_works_as_before(client, admin_token, db):
    res = add(client, admin_token)
    assert res.status_code == 201 and db.get(Venue, res.json()["id"]).website == ""
    assert add_missing(db, today_in_chicago()) == 0


def test_add_venue_refuses_a_bad_website(client, admin_token):
    assert add(client, admin_token, website="javascript:alert(1)").status_code == 422


def test_only_an_admin_can_read_a_website(client, user_token, web_pages, claude):
    web_pages[SITE] = ORG
    assert client.post("/venues/from-website", json={"url": SITE}).status_code == 401
    assert ask(client, user_token).status_code == 403
    assert claude.calls == 0


def test_the_limits_are_the_suggest_forms(client, admin_token, web_pages):
    web_pages[SITE] = ORG
    codes = [ask(client, admin_token).status_code for _ in range(fromlink.PER_MINUTE + 1)]
    assert codes == [200] * fromlink.PER_MINUTE + [429]


# ---- reading the page's data ----------------------------------------------------------------

def test_a_venue_inside_a_graph_or_a_web_page_is_found():
    page = ld('{"@graph": [{"@type": "WebSite", "name": "Home"}, {"@type": "WebPage", "name": "Home", "publisher": ' + ORG.split(">", 1)[1].split("</script")[0] + "}]}")
    assert venue_in(page)["name"] == "Lakeshore Playhouse" and venue_in(page)["zip_code"] == "60614"


def test_only_venue_types_count():
    page = ld('{"@type": "Person", "name": "A Director", "address": "9 Home St, Chicago, IL 60601"}')
    assert venue_in(page) == {"name": "", "street": "", "city": "", "state": "", "zip_code": ""}


def test_the_name_and_street_can_come_from_two_objects():
    page = ld('[{"@type": "TheaterGroup", "name": "Lakeshore Company"}, {"@type": "Place", "address": "1650 N Halsted St, Chicago, IL 60614"}]')
    assert venue_in(page) == {"name": "Lakeshore Company", "street": "1650 N Halsted St", "city": "Chicago", "state": "IL", "zip_code": "60614"}


def test_a_page_of_events_in_one_place_gives_that_place():
    place = '{"@type": "Place", "name": "Lakeshore Hall", "address": {"streetAddress": "1650 N Halsted St", "addressLocality": "Chicago", "postalCode": "60614"}}'
    one = '{"@type": "TheaterEvent", "name": "%s", "location": ' + place + "}"
    assert venue_in(ld("[" + one % "A" + "," + one % "B" + "]"))["name"] == "Lakeshore Hall"
    other = '{"@type": "MusicEvent", "name": "C", "location": {"@type": "Place", "name": "Far", "address": "2 Other St, Chicago, IL 60601"}}'
    assert venue_in(ld("[" + one % "A" + "," + other + "]"))["street"] == "", "two places: the page isn't one venue's"


@pytest.mark.parametrize("town, state, hood", [("Oak Park", "IL", "Oak Park, IL"), ("Evanston", "Illinois", "Evanston, IL"),
                                               ("Milwaukee", "WI", "Milwaukee, WI"), ("Milwaukee", "", "")])
def test_a_suburb_gets_the_form_of_neighborhood_the_rules_read(client, admin_token, web_pages, claude, town, state, hood):
    web_pages[SITE] = ld('{"@type": "Museum", "name": "M", "address": {"streetAddress": "1 Lake St", "addressLocality": "%s", '
                         '"addressRegion": "%s", "postalCode": "60301"}}' % (town, state))
    assert ask(client, admin_token).json()["neighborhood"] == hood


def test_the_rules_for_chicago_and_suburbs_apply_when_saving(client, admin_token):
    assert add(client, admin_token, neighborhood="Milwaukee, WI", website=SITE).status_code == 422


def test_what_a_page_says_is_checked(client, admin_token, web_pages, claude):
    claude.said = {"street": "See website", "city": "Chicago 60614", "zip_code": "unknown"}
    web_pages[SITE] = ld('{"@type": "Museum", "name": "M  <b>", "address": {"streetAddress": "TBA", "addressLocality": "Chicago"}}') + PLAIN
    got = ask(client, admin_token).json()
    assert claude.calls == 1 and got["address"] == "" and got["zip_code"] == "" and got["city"] == "Chicago"


def test_a_long_page_keeps_its_end_for_a_venue_read_only():
    html = "<body><p>" + "show " * MAX_CHARS + "</p><footer>1650 N Halsted St</footer></body>"
    assert "Halsted" in page_text(html, SITE, footers=True)[0] and len(page_text(html, SITE, footers=True)[0]) <= MAX_CHARS + 1
    assert "Halsted" not in page_text(html, SITE)[0]


def test_the_read_runs_in_its_own_process_by_a_name_that_can_be_sent():
    assert venuelink.fetch_venue.__module__ == "app.venuelink"
