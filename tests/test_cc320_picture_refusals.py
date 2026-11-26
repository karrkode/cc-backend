"""CC-320: pictures missed because of our own bugs, and the true reason where one is missing. Hand-made pages and a fake
net: nothing here touches a live site."""
import io
import json
import threading
import urllib.error
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select

from app.discovery import fetch as web
from app.discovery import pictures, readers
from app.discovery.readers import FoundEvent
from app.discovery.store import record
from app.models import DiscoveredItem, DiscoverySource, SourceKind

from .fake_net import route
from .test_event_pictures import _image, _page, stored  # noqa: F401 - fixture

NOW = datetime(2027, 1, 5, 15, tzinfo=UTC)
PAGE_HOST, CDN_HOST = "venue.example", "cdn.example"


class _Hosts(BaseHTTPRequestHandler):
    """ROUTES: (host, path) -> (status, content type, body). Anything not listed is a 404."""
    ROUTES: dict[tuple[str, str], tuple[int, str, bytes]] = {}
    asked: list[tuple[str, str]] = []

    def do_GET(self):
        host = self.headers["Host"]
        _Hosts.asked.append((host, self.path))
        status, ctype, body = _Hosts.ROUTES.get((host, self.path), (404, "text/html", b""))
        self.send_response(status)
        if status == 302:
            self.send_header("Location", body.decode()); body = b""
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def hosts(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hosts)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    web._robots.clear(); web._last_hit.clear(); _Hosts.asked.clear()
    _Hosts.ROUTES = {}
    monkeypatch.setattr(web, "MIN_GAP_SECONDS", 0)
    route(monkeypatch, server.server_port)
    yield _Hosts.ROUTES
    server.shutdown()
    web._robots.clear(); web._last_hit.clear()


def _jpeg(routes, host=CDN_HOST, path="/a.jpg"):
    routes[(host, path)] = (200, "image/jpeg", _image())


# --- 1: addresses outside ASCII ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("http://a.example/x y", "http://a.example/x%E2%80%AFy"),  # a narrow no-break space
    ("http://a.example/a×b?q=×", "http://a.example/a%C3%97b?q=%C3%97"),
    ("http://a.example/café", "http://a.example/caf%C3%A9"),
    ("http://a.example/a%20b?c=%C3%A9", "http://a.example/a%20b?c=%C3%A9"),  # already encoded: not encoded twice
    ("http://a.example/100%/í", "http://a.example/100%25/%C3%AD"),  # a stray % is encoded, a real one kept
    ("https://bücher.example/café", "https://xn--bcher-kva.example/caf%C3%A9"),  # the host as IDNA
    ("https://bücher.example:443/p", "https://xn--bcher-kva.example:443/p"),
    ("http://a.example/plain?x=1", "http://a.example/plain?x=1"),
])
def test_an_address_is_encoded_once(raw, want):
    assert web.normalise_url(raw) == want
    assert web.normalise_url(want) == want  # and again changes nothing


def test_an_address_that_cant_be_written_is_a_bad_address():
    with pytest.raises(web.BadAddress):
        web.normalise_url("http://" + "ü" * 70 + ".example/")  # an IDNA label over 63 characters


def test_a_page_with_a_non_ascii_address_is_fetched_and_the_checked_address_is_the_requested_one(hosts, monkeypatch):
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(PAGE_HOST, "/caf%C3%A9%E2%80%AF/%C3%97")] = (200, "text/html", b"<html>ok</html>")
    checked, opened = [], []
    real_polite, real_open = web._polite, web._open
    monkeypatch.setattr(web, "_polite", lambda url, *a, **k: (checked.append(url), real_polite(url, *a, **k))[1])
    monkeypatch.setattr(web, "_open", lambda url, *a, **k: (opened.append(url), real_open(url, *a, **k))[1])
    page = web.fetch(f"http://{PAGE_HOST}/café /×")
    assert page.status == 200 and page.text == "<html>ok</html>"
    want = f"http://{PAGE_HOST}/caf%C3%A9%E2%80%AF/%C3%97"
    assert checked[0] == opened[-1] == want  # (opened[0] is robots.txt)


def test_a_redirect_to_an_encoded_address_is_followed(hosts):
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(PAGE_HOST, "/old")] = (302, "text/html", b"/new%C3%A9")  # the body of a 302 here is where it goes
    hosts[(PAGE_HOST, "/new%C3%A9")] = (200, "text/html", b"<html>moved</html>")
    assert web.fetch(f"http://{PAGE_HOST}/old").text == "<html>moved</html>"


# --- 2 and 3: robots.txt on a picture server ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [401, 403, 404, 410, 418])
def test_a_picture_server_with_no_usable_robots_txt_has_no_rules(hosts, code):
    hosts[(CDN_HOST, "/robots.txt")] = (code, "text/plain", b"")
    _jpeg(hosts)
    got = web.fetch_file(f"http://{CDN_HOST}/a.jpg", "image/*", 10_000_000, page_url=f"http://{PAGE_HOST}/show")
    assert got.content_type == "image/jpeg" and (CDN_HOST, "/a.jpg") in _Hosts.asked


@pytest.mark.parametrize("code", [429, 500, 503])
def test_a_picture_server_that_is_busy_means_not_now(hosts, code):
    hosts[(CDN_HOST, "/robots.txt")] = (code, "text/plain", b"")
    _jpeg(hosts)
    with pytest.raises(web.FetchError) as e:
        web.fetch_file(f"http://{CDN_HOST}/a.jpg", "image/*", 10_000_000, page_url=f"http://{PAGE_HOST}/show")
    assert not isinstance(e.value, web.Blocked) and CDN_HOST in str(e.value)
    assert (CDN_HOST, "/a.jpg") not in _Hosts.asked  # and the picture isn't taken this time


def test_a_picture_server_that_times_out_means_not_now(hosts, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(web, "_opener", lambda: type("O", (), {"open": staticmethod(boom)})())
    with pytest.raises(web.FetchError, match="didn't answer in time"):
        web.fetch_file(f"http://{CDN_HOST}/a.jpg", "image/*", 1000, page_url=f"http://{PAGE_HOST}/show")


@pytest.mark.parametrize("code", [401, 403])
def test_a_picture_on_the_pages_own_server_keeps_the_page_rule(hosts, code):
    hosts[(PAGE_HOST, "/robots.txt")] = (code, "text/plain", b"")
    _jpeg(hosts, PAGE_HOST)
    with pytest.raises(web.Blocked, match=f"{PAGE_HOST} refuses automated visitors"):
        web.fetch_file(f"http://{PAGE_HOST}/a.jpg", "image/*", 10_000_000, page_url=f"http://{PAGE_HOST}/show")
    assert (PAGE_HOST, "/a.jpg") not in _Hosts.asked


def test_a_page_keeps_the_page_rule_even_after_a_picture_server_was_treated_leniently(hosts):
    hosts[(CDN_HOST, "/robots.txt")] = (403, "text/plain", b"")
    _jpeg(hosts)
    hosts[(CDN_HOST, "/page")] = (200, "text/html", b"<html></html>")
    web.fetch_file(f"http://{CDN_HOST}/a.jpg", "image/*", 10_000_000, page_url=f"http://{PAGE_HOST}/show")
    with pytest.raises(web.Blocked):  # as a page of its own, the 403 is still a refusal
        web.fetch(f"http://{CDN_HOST}/page")


def test_a_readable_robots_txt_that_disallows_us_is_obeyed_for_a_picture(hosts):
    hosts[(CDN_HOST, "/robots.txt")] = (200, "text/plain", b"User-agent: *\nDisallow: /private/\n")
    _jpeg(hosts, path="/private/a.jpg"); _jpeg(hosts, path="/open/a.jpg")
    page = f"http://{PAGE_HOST}/show"
    with pytest.raises(web.Blocked, match=f"{CDN_HOST}'s robots.txt"):
        web.fetch_file(f"http://{CDN_HOST}/private/a.jpg", "image/*", 10_000_000, page_url=page)
    assert web.fetch_file(f"http://{CDN_HOST}/open/a.jpg", "image/*", 10_000_000, page_url=page).data


def test_the_ai_opt_out_still_applies_to_anything_sent_to_claude(hosts):
    hosts[(PAGE_HOST, "/robots.txt")] = (200, "text/plain", b"User-agent: ClaudeBot\nDisallow: /\n")
    hosts[(PAGE_HOST, "/p")] = (200, "text/html", b"<html></html>")
    with pytest.raises(web.Blocked, match="opts out of AI crawlers"):
        web.fetch(f"http://{PAGE_HOST}/p", ai=True)
    assert web.fetch(f"http://{PAGE_HOST}/p", ai=False).status == 200


def test_robots_rules_written_with_non_ascii_characters_still_apply():
    for rule in ("/\u00e9v\u00e9nements/", "/%c3%a9v%c3%a9nements/", "/%C3%A9v%C3%A9nements/"):
        rules = web.parse_robots(f"User-agent: *\nDisallow: {rule}\n")
        assert not rules.allows("/%C3%A9v%C3%A9nements/x") and not rules.allows("/\u00e9v\u00e9nements/x")
        assert rules.allows("/autre/x")
    assert not web.parse_robots("User-agent: *\nDisallow: /a%2fb\n").allows("/a%2Fb")


@pytest.mark.parametrize("page, pic, other", [
    ("http://v.example/p", "http://V.example./a.jpg", False), ("https://v.example/p", "https://v.example:443/a.jpg", False),
    ("http://v.example/p", "http://cdn.example/a.jpg", True), ("http://v.example/p", "https://v.example/a.jpg", True),
    ("", "http://cdn.example/a.jpg", False),
])
def test_the_pages_own_server_is_told_from_a_separate_one(page, pic, other):
    assert web._picture_server(pic, page) is other


# --- 4: ticket words ---------------------------------------------------------------------------------------------------

def test_ticket_words_match_whole_words():
    from app.discovery.tickets import links_of
    html = ('<a href="https://www.facebook.com/theater">Facebook</a><a href="/book_now">go</a>'
            '<a href="/x">Book tickets</a><a href="/y">Booking policy</a><a href="/z">Events</a>')
    got = dict(links_of(html, "https://venue.example/show"))
    assert "https://www.facebook.com/theater" not in got
    assert "https://venue.example/x" in got and "https://venue.example/book_now" in got
    assert "https://venue.example/z" in got and "https://venue.example/y" not in got


# --- 5: the reason is the true one -------------------------------------------------------------------------------------

def _source(db, reader="claude", page="https://venue.example/season"):
    s = DiscoverySource(name="Venue", kind=SourceKind.theater, pages=[page], reader=reader, venue_name="Venue",
                        neighborhood="Loop", every_days=30, page_state={}, venue_hours={}, unchanged_streak=0,
                        last_status="", last_error="")
    db.add(s); db.commit()
    return s


def _item(db, source, title="The Seagull", url=f"http://{PAGE_HOST}/seagull", **cols):
    record(db, source, FoundEvent(title=title, url=url, kind="production", category="Theater",
                                  begin_date="2027-02-01", end_date="2027-03-01"), "")
    db.commit()
    it = db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == title))
    for k, v in cols.items():
        setattr(it, k, v)
    db.commit()
    return it


def _on_venue(db):
    return _source(db, page=f"http://{PAGE_HOST}/season")


def test_a_failing_ticket_link_never_replaces_the_pages_own_reason(db, hosts, stored):
    src = _on_venue(db)
    it = _item(db, src)
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(PAGE_HOST, "/seagull")] = (200, "text/html", _page('<a href="/tickets">Tickets</a>'))
    hosts[(PAGE_HOST, "/tickets")] = (403, "text/html", b"")  # refused: must not become the reason
    [(_, status, _)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "none" and it.image_note == "The page names no picture"


def test_a_refusal_names_the_site_and_not_found_is_only_for_404_and_410(db, hosts, stored):
    src = _on_venue(db)
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    for path, code in (("/gone", 410), ("/missing", 404), ("/down", 500), ("/nope", 403)):
        hosts[(PAGE_HOST, path)] = (code, "text/html", b"")
    with pytest.raises(web.FetchError, match=r"page not found \(HTTP 410\)"):
        web.fetch(f"http://{PAGE_HOST}/gone")
    with pytest.raises(web.FetchError, match=r"page not found \(HTTP 404\)"):
        web.fetch(f"http://{PAGE_HOST}/missing")
    with pytest.raises(web.FetchError) as e:
        web.fetch(f"http://{PAGE_HOST}/down")
    assert "not found" not in str(e.value) and f"{PAGE_HOST} answered HTTP 500" in str(e.value)
    it = _item(db, src, url=f"http://{PAGE_HOST}/nope")
    [(_, status, _)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "none" and it.image_note == f"Refused: {PAGE_HOST} refused the request (HTTP 403)"


def test_a_picture_on_a_separate_server_with_a_403_robots_txt_is_found(db, hosts, stored):
    src = _on_venue(db)
    it = _item(db, src)
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(PAGE_HOST, "/seagull")] = (200, "text/html", _page(f'<meta property="og:image" content="http://{CDN_HOST}/a.jpg">'))
    hosts[(CDN_HOST, "/robots.txt")] = (403, "text/plain", b"")
    _jpeg(hosts)
    [(_, status, note)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "ok" and it.image_key and CDN_HOST in note


def test_a_picture_with_a_non_ascii_address_is_found(db, hosts, stored):
    src = _on_venue(db)
    it = _item(db, src)
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(PAGE_HOST, "/seagull")] = (200, "text/html", _page(f'<meta property="og:image" content="http://{CDN_HOST}/a× b.jpg">'))
    hosts[(CDN_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(CDN_HOST, "/a%C3%97%E2%80%AFb.jpg")] = (200, "image/jpeg", _image())
    [(_, status, _)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "ok" and it.image_key


# --- 6: the Art Institute's API -----------------------------------------------------------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_aic_image_url_asks_the_api_for_the_id_in_the_address(monkeypatch):
    seen = []

    def urlopen(req, timeout=None):
        seen.append((req.full_url, req.get_method()))
        return _Resp(json.dumps({"data": {"id": 5712, "image_url": "https://www.artic.edu/iiif/2/x/full/843,/0/default.jpg"}}).encode())
    monkeypatch.setattr(readers.urllib.request, "urlopen", urlopen)
    assert readers.aic_image_url("https://www.artic.edu/exhibitions/5712/some-show").endswith("default.jpg")
    assert seen == [("https://api.artic.edu/api/v1/exhibitions/5712?fields=id,image_url", "GET")]
    assert readers.aic_image_url("https://www.artic.edu/events/99/x") is not None
    assert readers.aic_image_url("https://evil.example/exhibitions/5712/x") is None  # only their own addresses
    assert readers.aic_image_url("https://www.artic.edu/exhibitions/abc") is None
    assert len(seen) == 2  # nothing asked for the two that aren't theirs


def test_aic_image_url_is_none_when_the_api_has_no_usable_picture(monkeypatch):
    for body in ({"data": {"image_url": None}}, {"data": {"image_url": "http://plain.example/a.jpg"}}, {}):
        monkeypatch.setattr(readers.urllib.request, "urlopen", lambda req, timeout=None, b=body: _Resp(json.dumps(b).encode()))
        assert readers.aic_image_url("https://www.artic.edu/exhibitions/1/x") is None


def test_an_art_institute_item_gets_its_picture_from_the_api_not_its_page(db, hosts, stored, monkeypatch):
    src = _source(db, reader="aic_api", page="https://www.artic.edu/exhibitions")
    it = _item(db, src, url="https://www.artic.edu/exhibitions/5712/some-show")
    hosts[(CDN_HOST, "/robots.txt")] = (404, "text/plain", b"")
    _jpeg(hosts)
    monkeypatch.setattr(pictures, "aic_image_url", lambda u: f"http://{CDN_HOST}/a.jpg")
    monkeypatch.setattr(web, "fetch", lambda *a, **k: pytest.fail("the page isn't fetched"))
    [(_, status, note)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "ok" and "Art Institute's API" in note
    assert it.image_key and it.image_source_url == "https://www.artic.edu/exhibitions/5712/some-show"


def test_an_art_institute_picture_still_meets_the_picture_rules(db, hosts, stored, monkeypatch):
    src = _source(db, reader="aic_api", page="https://www.artic.edu/exhibitions")
    it = _item(db, src, url="https://www.artic.edu/exhibitions/5712/some-show")
    hosts[(CDN_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(CDN_HOST, "/a.jpg")] = (200, "image/jpeg", _image(size=(100, 100)))  # too small: an icon
    monkeypatch.setattr(pictures, "aic_image_url", lambda u: f"http://{CDN_HOST}/a.jpg")
    [(_, status, _)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "none" and it.image_key is None and "too small" in it.image_note


def test_an_art_institute_item_the_api_no_longer_has_is_a_reason_not_a_retry(db, hosts, stored, monkeypatch):
    src = _source(db, reader="aic_api", page="https://www.artic.edu/exhibitions")
    it = _item(db, src, url="https://www.artic.edu/exhibitions/5712/some-show")
    monkeypatch.setattr(pictures, "aic_image_url", lambda u: (_ for _ in ()).throw(
        urllib.error.HTTPError(u, 404, "Not Found", {}, None)))
    [(_, status, _)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "none" and it.image_note == "The Art Institute's API no longer has this one"


def test_an_art_institute_api_with_no_picture_or_no_answer(db, hosts, stored, monkeypatch):
    src = _source(db, reader="aic_api", page="https://www.artic.edu/exhibitions")
    it = _item(db, src, url="https://www.artic.edu/exhibitions/5712/some-show")
    monkeypatch.setattr(pictures, "aic_image_url", lambda u: (_ for _ in ()).throw(urllib.error.URLError("down")))
    [(_, status, _)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "error" and it.image_note.startswith("Still trying: the Art Institute's API didn't answer")
    monkeypatch.setattr(pictures, "aic_image_url", lambda u: None)
    [(_, status, _)] = pictures.add_item_pictures(db, NOW + timedelta(hours=2))
    db.refresh(it)
    assert status == "none" and it.image_note == "The Art Institute's API has no picture for this one"


# --- 7: one more look for what an older finder missed ------------------------------------------------------------------

def _missed(db, src, title, **cols):
    base = dict(image_tries=pictures.MAX_TRIES, image_finder=0, image_note="Refused: venue.example refused the request (HTTP 403)",
                image_checked_at=NOW - timedelta(days=3))
    return _item(db, src, title=title, url=f"http://{PAGE_HOST}/{title}", **{**base, **cols})


def test_an_item_an_older_finder_missed_is_due_once_more_and_then_not(db, hosts, stored):
    src = _on_venue(db)
    it = _missed(db, src, "seagull")
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(PAGE_HOST, "/seagull")] = (200, "text/html", _page())  # no picture: the look is final
    assert pictures.due_items(db, NOW) == [it]
    [(_, status, _)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "none" and it.image_finder == pictures.FINDER_VERSION and it.image_tries == pictures.MAX_TRIES
    assert it.image_note == "The page names no picture"
    assert pictures.due_items(db, NOW + timedelta(days=2)) == []  # one more look, not a loop


def test_the_extra_look_is_stamped_before_the_fetch_so_a_killed_look_isnt_repeated(db, hosts, stored, monkeypatch):
    src = _on_venue(db)
    it = _missed(db, src, "seagull")
    seen = {}

    def boom(*a, **k):
        db.expire_all()
        seen["finder"] = db.get(DiscoveredItem, it.id).image_finder
        raise KeyboardInterrupt("killed")
    monkeypatch.setattr(pictures, "add_item_picture", boom)
    with pytest.raises(KeyboardInterrupt):
        pictures.look_for_item(db, it, NOW)
    assert seen["finder"] == pictures.FINDER_VERSION


def test_the_extra_look_leaves_alone_what_it_must(db, hosts, stored):
    src = _on_venue(db)
    current = _missed(db, src, "current", image_finder=pictures.FINDER_VERSION)  # already looked at by this finder
    admin = _missed(db, src, "admin", image_override=True)  # an admin's choice is never replaced
    has = _missed(db, src, "has", image_key="event-images/found-1/x.webp")
    no_page = _missed(db, src, "nopage", image_note="The event has no page of its own on the source's site")
    done = _missed(db, src, "done", status="approved")
    assert pictures.due_items(db, NOW) == []
    assert {current.image_key, admin.image_key, no_page.image_key, done.image_key} == {None}
    assert has.image_key


def test_an_extra_look_that_fails_is_still_only_one_look(db, hosts, stored):
    src = _on_venue(db)
    it = _missed(db, src, "seagull")
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(PAGE_HOST, "/seagull")] = (500, "text/html", b"")
    [(_, status, _)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert status == "error" and it.image_tries == pictures.MAX_TRIES and it.image_note.startswith("Gave up")
    assert pictures.due_items(db, NOW + timedelta(days=2)) == []


def test_new_items_go_before_the_extra_looks(db, hosts, stored):
    src = _on_venue(db)
    for n in range(10):
        _missed(db, src, f"old{n}")
    fresh = _item(db, src, title="fresh", url=f"http://{PAGE_HOST}/fresh")  # never looked at: a higher id than the backlog
    assert fresh in pictures.due_items(db, NOW)


def test_the_extra_looks_are_ten_an_hour(db, hosts, stored):
    src = _on_venue(db)
    for n in range(14):
        _missed(db, src, f"s{n}")
    assert len(pictures.due_items(db, NOW)) == pictures.ITEM_PER_RUN == 10
