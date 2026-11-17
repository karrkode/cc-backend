"""CC-287: every HTML page the API renders carries the page CSP and refuses framing, and nothing the pages show can
turn into markup."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from sqlalchemy import select

from app import pageheaders
from app.models import Attended, User
from app.roundups import ROUNDUPS, set_intro
from app.stats import today_in_chicago

from .conftest import register

HOSTILE = '"><img src=x onerror=alert(1)><script>alert(1)</script>\'&'
WORD = '"><img/src=x/onerror=alert(1)>\'&'  # a take's words are split on spaces, so one word


def assert_page_headers(r):
    assert r.headers["content-type"].startswith("text/html"), r.url
    assert r.headers["content-security-policy"] == pageheaders.PAGE_CSP, r.url
    assert r.headers["x-frame-options"] == "DENY", r.url
    assert "frame-ancestors 'none'" in pageheaders.PAGE_CSP
    assert len(r.headers.get_list("content-security-policy")) == 1  # one policy, not two


def test_every_html_page_has_the_csp_and_refuses_framing(client, make_event):
    ev = make_event(title="Hamlet")
    for path in (f"/e/{ev.id}-hamlet", "/e/999999-nothing", "/chicago/", "/chicago/no-such-roundup",
                 *(f"/chicago/{r.slug}" for r in ROUNDUPS)):
        assert_page_headers(client.get(path))


def test_a_redirect_is_not_html_and_json_and_text_are_left_alone(client, make_event):
    ev = make_event(title="Hamlet")
    for path in (f"/e/{ev.id}", "/chicago", "/health", "/events", "/robots.txt", "/sitemap.xml"):
        r = client.get(path, follow_redirects=False)
        assert not r.headers["content-type"].startswith("text/html") if "content-type" in r.headers else True, path
        assert "content-security-policy" not in r.headers and "x-frame-options" not in r.headers, path


def test_a_route_that_sets_its_own_policy_keeps_it():
    import asyncio

    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html"), (b"content-security-policy", b"default-src 'none'")]})
        await send({"type": "http.response.body", "body": b""})

    async def send(m):
        sent.append(m)

    asyncio.run(pageheaders.PageHeaders(app)({"type": "http", "path": "/x"}, None, send))
    values = [v for k, v in sent[0]["headers"] if k == b"content-security-policy"]
    assert values == [b"default-src 'none'"]


class Parsed(HTMLParser):
    """What a browser would make of the page: its tags, attributes and text."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags, self.attr_names, self.text, self.attr_values, self.scripts = [], set(), [], [], []
        self._in_script = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self._in_script = tag == "script"
        for k, v in attrs:
            self.attr_names.add(k)
            self.attr_values.append(v or "")

    def handle_endtag(self, tag):
        self._in_script = False

    def handle_data(self, data):
        (self.scripts if self._in_script else self.text).append(data)


SAFE_TAGS = {"html", "head", "meta", "link", "style", "script", "title", "body", "header", "div", "a", "svg", "polygon",
             "main", "p", "h1", "h2", "dl", "dt", "dd", "section", "ol", "ul", "li", "span", "b", "nav", "footer", "i"}


def test_hostile_values_show_as_text_only_on_event_and_roundup_pages(client, make_event, db):
    """CC-287: markup and quotes in every value a page shows come out as text, never as a tag or an attribute."""
    today = today_in_chicago()
    ev = make_event(title="T" + HOSTILE, venue_name="V" + HOSTILE, neighborhood="N" + HOSTILE,
                    begin=today - timedelta(days=1), end=today + timedelta(days=2), cost_text="C" + HOSTILE)
    ev.venue.address, ev.venue.city, ev.venue.zip_code = "A" + HOSTILE, "Y" + HOSTILE, "Z" + HOSTILE
    ev.url = "https://example.org/" + HOSTILE
    register(client, "dolphin@example.com", name="D" + HOSTILE)
    user = db.scalar(select(User).where(User.email == "dolphin@example.com"))
    user.dolphins = True  # Yours truly: the one take the public page shows
    db.add(Attended(user_id=user.id, event_id=ev.id, words="W" + WORD + " x y", review="R" + HOSTILE, recommend=True,
                    shared_at=datetime.now(timezone.utc)))
    for r in ROUNDUPS:
        set_intro(db, r.slug, "I" + HOSTILE)
    db.commit()

    pages = {"event": client.get(f"/e/{ev.id}-t").text, "index": client.get("/chicago/").text}
    pages.update({r.slug: client.get(f"/chicago/{r.slug}").text for r in ROUNDUPS})
    for name, html in pages.items():
        p = Parsed(html)
        assert set(p.tags) <= SAFE_TAGS, (name, set(p.tags) - SAFE_TAGS)
        assert not [a for a in p.attr_names if a.startswith("on") or a in ("srcdoc", "src")], name
        assert len(p.scripts) == 1 + (name == "event"), name  # the theme script, and the event's JSON-LD

    ev_page = Parsed(pages["event"])
    text = "\n".join(ev_page.text)
    for prefix in "TVNCAY":  # title, venue, neighborhood, price, street, city
        assert prefix + HOSTILE in text, prefix
    assert "W" + WORD in text  # a take's words
    assert "R" + HOSTILE not in text and "R&quot;" not in pages["event"]  # a review is never shown
    assert any(v == "https://example.org/" + HOSTILE for v in ev_page.attr_values)  # the ticket link, inside its attribute
    assert any(v.startswith("Theater at V" + HOSTILE) for v in ev_page.attr_values)  # the description meta

    for r in ROUNDUPS:
        text = "\n".join(Parsed(pages[r.slug]).text)
        assert "I" + HOSTILE in text, r.slug
    some = next(Parsed(h) for n, h in pages.items() if n not in ("event", "index") and "T&quot;" in h)
    text = "\n".join(some.text)
    assert all(x + HOSTILE in text for x in "TVNC")  # title, venue, neighborhood, price on a roundup row
