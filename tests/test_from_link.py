"""CC-124: suggesting an event by its link: a draft from the page's event data, or Claude's reading of it."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import fromlink
from app.discovery import fetch as web
from app.discovery.budget import budget_state, set_monthly_budget
from app.discovery.fetch import Page, Robots
from app.discovery.readers import FoundEvent, Performance
from app.models import DiscoveryCheck, Venue
from app.stats import today_in_chicago

from .conftest import auth
from .test_discovery import FakeClaude

DAY = today_in_chicago() + timedelta(days=20)
LD_PAGE = """<html><head><script type="application/ld+json">{"@context": "https://schema.org", "@type": "TheaterEvent",
"name": "The Seagull", "startDate": "%s", "endDate": "%s",
"location": {"@type": "Place", "name": "Steppenwolf Theatre", "address": {"addressLocality": "Chicago"}},
"offers": [{"@type": "Offer", "price": "35"}, {"@type": "Offer", "price": "90"}]}</script></head>
<body><h1>The Seagull</h1></body></html>""" % (DAY.isoformat(), (DAY + timedelta(days=30)).isoformat())
PLAIN_PAGE = "<html><body><h1>Jazz at the Green Mill</h1><p>Friday at 9 PM, $15 at the door.</p></body></html>"


@pytest.fixture
def web_pages(monkeypatch):
    """Pages by url; every host is public unless a test says otherwise; robots.txt allows everything."""
    served = {}

    def fake_fetch(url, etag="", last_modified="", ai=True, check=None, timeout=25):
        if check:
            check(url)
        got = served[url]
        if isinstance(got, Exception):
            raise got
        return Page(url=url, status=200, text=got)
    monkeypatch.setattr(web, "fetch", fake_fetch)
    monkeypatch.setattr(web, "robots_for", lambda url, check=None, timeout=25: Robots([], [], 0.0, served.get("ai_opt_out", False)))
    monkeypatch.setattr(fromlink, "_addresses", lambda host, port: ["93.184.215.14"])
    return served


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude([FoundEvent(title="Jazz at the Green Mill", kind="concert", category="Music", venue_name="Green Mill",
                                  cost_text="$15", min_price=15, begin_date=DAY.isoformat(), end_date=DAY.isoformat(),
                                  performances=[Performance(date=DAY.isoformat(), time="21:00")])])
    monkeypatch.setattr(fromlink, "claude_client", lambda: fake)
    return fake


def ask(client, token, url):
    return client.post("/events/from-link", json={"url": url}, headers=auth(token))


def test_event_data_fills_the_draft_without_claude(client, user_token, db, web_pages, claude):
    db.add(Venue(name="Steppenwolf Theatre", neighborhood="Lincoln Park")); db.commit()
    web_pages["https://steppenwolf.example/seagull"] = LD_PAGE
    r = ask(client, user_token, "https://steppenwolf.example/seagull")
    assert r.status_code == 200, r.text
    d = r.json()
    assert claude.calls == 0 and d["read_by"] == "page"
    assert (d["title"], d["category"], d["venue"], d["neighborhood"]) == ("The Seagull", "Theater", "Steppenwolf Theatre", "Lincoln Park")
    assert (d["begin_date"], d["cost_text"], d["url"]) == (DAY.isoformat(), "$35–$90", "https://steppenwolf.example/seagull")
    assert d["missing"] == []
    check = db.scalar(select(DiscoveryCheck))
    assert (check.source_id, check.what, check.status, check.output_tokens) == (None, "link", "ok", 0)
    assert client.get("/events", params={"q": "Seagull"}).json()["total"] == 0  # nothing saved


def test_missing_fields_are_named(client, user_token, web_pages, claude):
    web_pages["https://x.example/show"] = LD_PAGE.replace('"offers"', '"x"').replace("Steppenwolf Theatre", "Somewhere New")
    d = ask(client, user_token, "https://x.example/show").json()
    assert d["missing"] == ["neighborhood", "price"]  # a Chicago venue we don't know: its neighborhood isn't on the page


def test_no_event_data_is_read_by_claude_and_costs_the_budget(client, user_token, db, web_pages, claude):
    web_pages["https://greenmill.example/jazz"] = PLAIN_PAGE
    before = budget_state(db)[1]
    r = ask(client, user_token, "https://greenmill.example/jazz")
    assert r.status_code == 200, r.text
    d = r.json()
    assert claude.calls == 1 and d["read_by"] == "claude"
    assert (d["title"], d["category"], d["venue"], d["cost_text"]) == ("Jazz at the Green Mill", "Music", "Green Mill", "$15")
    assert d["run_dates"] == [{"date": DAY.isoformat(), "time": "21:00"}]  # one dated show: a fixed run
    check = db.scalar(select(DiscoveryCheck))
    assert (check.source_id, check.what, check.input_tokens, check.output_tokens, check.pages_read) == (None, "link", 1000, 200, 1)
    assert budget_state(db)[1] > before


def test_used_up_budget_tries_event_data_only(client, user_token, db, web_pages, claude):
    set_monthly_budget(db, 0.00001); db.add(DiscoveryCheck(source_id=None, what="link", status="ok", output_tokens=1000)); db.commit()
    assert budget_state(db)[0] == "paused"
    web_pages["https://greenmill.example/jazz"] = PLAIN_PAGE
    r = ask(client, user_token, "https://greenmill.example/jazz")
    assert r.status_code == 422 and "couldn't find an event" in r.json()["detail"]
    assert claude.calls == 0
    web_pages["https://steppenwolf.example/seagull"] = LD_PAGE
    assert ask(client, user_token, "https://steppenwolf.example/seagull").status_code == 200  # structured data still works


def test_ai_opt_out_means_no_claude(client, user_token, web_pages, claude):
    web_pages["https://greenmill.example/jazz"] = PLAIN_PAGE
    web_pages["ai_opt_out"] = True
    assert ask(client, user_token, "https://greenmill.example/jazz").status_code == 422
    assert claude.calls == 0


def test_unreadable_pages_say_so(client, user_token, db, web_pages, claude):
    web_pages["https://shy.example/e"] = web.Blocked("robots.txt asks crawlers not to read this page")
    web_pages["https://down.example/e"] = web.FetchError("HTTP 503")
    r = ask(client, user_token, "https://shy.example/e")
    assert r.status_code == 422 and r.json()["detail"] == "That site doesn't let us read its pages automatically."
    r = ask(client, user_token, "https://down.example/e")
    assert r.status_code == 422 and r.json()["detail"] == "We couldn't open that page."
    assert [c.status for c in db.scalars(select(DiscoveryCheck).order_by(DiscoveryCheck.id))] == ["blocked", "error"]
    claude.events = []  # Claude finds no event on it
    web_pages["https://blog.example/post"] = "<p>Ten things to do this fall</p>"
    r = ask(client, user_token, "https://blog.example/post")
    assert r.status_code == 422 and "couldn't find an event" in r.json()["detail"]


@pytest.mark.parametrize("url,addr", [
    ("http://localhost/admin", "127.0.0.1"), ("http://127.0.0.1:80/", "127.0.0.1"), ("http://10.0.0.5/", "10.0.0.5"),
    ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"), ("http://[::1]/", "::1"),
    ("http://internal.example/", "192.168.1.20"), ("http://mapped.example/", "::ffff:10.0.0.1"),
    ("ftp://files.example/x", "93.184.215.14"), ("https://site.example:8443/", "93.184.215.14"),
    ("https://user:pw@site.example/", "93.184.215.14"), ("file:///etc/passwd", "127.0.0.1")])
def test_private_and_odd_links_are_refused(client, user_token, monkeypatch, url, addr):
    monkeypatch.setattr(fromlink, "_addresses", lambda host, port: [addr])
    monkeypatch.setattr(web, "fetch", lambda *a, **k: pytest.fail("fetched a refused link"))
    r = ask(client, user_token, url)
    assert r.status_code == 422 and r.json()["detail"].startswith("We can't read that link")


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(fromlink, "_addresses", lambda host, port: ["10.0.0.1" if host == "inside.example" else "93.184.215.14"])
    hop = web._CheckedRedirects(fromlink.check_public)
    with pytest.raises(fromlink.PrivateAddress):
        hop.redirect_request(None, None, 302, "Found", {}, "http://inside.example/secret")


def test_signed_in_and_rate_limited(client, user_token, web_pages, claude, monkeypatch):
    assert client.post("/events/from-link", json={"url": "https://x.example/e"}).status_code == 401
    web_pages["https://steppenwolf.example/seagull"] = LD_PAGE
    codes = [ask(client, user_token, "https://steppenwolf.example/seagull").status_code for _ in range(fromlink.PER_MINUTE + 1)]
    assert codes == [200] * fromlink.PER_MINUTE + [429]
    fromlink.minute_limiter.reset(); fromlink.day_limiter.reset()
    monkeypatch.setattr(fromlink, "PER_DAY", 2)
    codes = [ask(client, user_token, "https://steppenwolf.example/seagull").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_past_shows_are_left_out_and_long_runs_use_dates(db):
    today = today_in_chicago()
    shows = [Performance(date=(today + timedelta(days=i)).isoformat(), time="19:30") for i in range(-2, 40)]
    e = FoundEvent(title="Long Run", kind="production", performances=shows, begin_date=today.isoformat(), end_date=(today + timedelta(days=39)).isoformat())
    d = fromlink._draft(db, e, "https://x.example", {}, "claude", today, 0)
    assert d["run_dates"] == []  # more than a fixed run takes: the opening and closing dates instead
    e.performances = shows[:5]
    d = fromlink._draft(db, e, "https://x.example", {}, "claude", today, 0)
    assert [r["date"] for r in d["run_dates"]] == [s.date for s in shows[2:5]]


def test_a_suburb_gets_its_town():
    from app.discovery.jsonld import event_places
    page = LD_PAGE.replace('"addressLocality": "Chicago"', '"addressLocality": "Evanston"')
    assert event_places(page)["the seagull"] == {"venue": "Steppenwolf Theatre", "town": "Evanston", "category": "Theater",
                                                 "end_given": True}


def test_a_start_date_only_or_a_placeholder_date_is_not_a_run(client, user_token, web_pages, claude):
    web_pages["https://goodman.example/carol"] = LD_PAGE.replace(', "endDate": "%s"' % (DAY + timedelta(days=30)).isoformat(), "")
    d = ask(client, user_token, "https://goodman.example/carol").json()
    assert (d["begin_date"], d["end_date"]) == (DAY.isoformat(), None)  # not closing on its opening night
    web_pages["https://goodman.example/quince"] = LD_PAGE.replace(DAY.isoformat(), "1970-01-01").replace(
        (DAY + timedelta(days=30)).isoformat(), "1970-01-01")
    d = ask(client, user_token, "https://goodman.example/quince").json()
    assert (d["begin_date"], d["end_date"]) == (None, None) and "dates" in d["missing"]


def test_a_venue_site_known_to_discovery_gives_its_venue(client, user_token, db, web_pages, claude):
    from .test_discovery import make_source
    make_source(db)  # Goodman, at goodman.example, with its venue in the Loop
    web_pages["https://goodman.example/carol"] = LD_PAGE.replace("Steppenwolf Theatre", "albert").replace('"TheaterEvent"', '"Event"')
    d = ask(client, user_token, "https://goodman.example/carol").json()
    assert (d["venue"], d["neighborhood"], d["category"]) == ("Goodman Theatre", "Loop", "Theater")
