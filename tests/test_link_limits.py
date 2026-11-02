"""CC-182: reading a member's link is bounded in time and size, runs apart from the API's requests, and still only
reaches public hosts."""
import json
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import fromlink
from app.discovery import fetch as web
from app.discovery.jsonld import MAX_DATA, events_in, page_titles, visible_title
from app.discovery.pagetext import page_text

from . import link_workers
from .conftest import auth
from .test_from_link import LD_PAGE, web_pages  # noqa: F401 - fixture

SIZE = 3_000_000  # fromlink.MAX_BYTES: the most of a member's page that's downloaded


def ld(event: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(event)}</script>'


def show(**extra) -> dict:
    return {"@type": "TheaterEvent", "name": "The Seagull", "startDate": "2030-01-05T19:30", **extra}


# --- parsing in time proportional to the page ---

@pytest.mark.parametrize("page", ["<script " * (SIZE // 8), "<script >" * (SIZE // 9), "<title>" * (SIZE // 7),
                                  "<meta " * (SIZE // 6) + ">", '<script type="application/ld+json">' * (SIZE // 36),
                                  "<script" + " " * SIZE + ">"])
def test_pages_without_closing_tags_are_read_quickly(page):
    started = time.monotonic()
    assert events_in(page, "https://x.example/", "") == []
    visible_title("The Seagull", page)
    page_titles(page)
    assert time.monotonic() - started < 3


def test_page_text_with_unclosed_addresses_is_read_quickly():
    started = time.monotonic()
    page_text("&lt;http://x" * (SIZE // 12), "https://x.example/")
    assert time.monotonic() - started < 3


def test_titles_are_still_found():
    page = ('<html><head><title>The Seagul - Steppenwolf</title><meta property="og:title" content="The Seagull 2030">'
            + ld(show()) + "</head></html>")
    assert page_titles(page)[:2] == ["The Seagul - Steppenwolf", "The Seagull 2030"]
    assert [e.title for e in events_in(page, "https://x.example/", "")] == ["The Seagull"]
    assert visible_title("The Seagul", page) == "The Seagul"


def test_at_most_a_megabyte_of_event_data_is_parsed(monkeypatch):
    parsed = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda text: parsed.append(len(text)) or real(text))
    filler = ld({"@type": "Thing", "description": "x" * (MAX_DATA // 3)})
    page = ld(show(name="First")) + filler * 5 + ld(show(name="Last"))
    assert [e.title for e in events_in(page, "https://x.example/", "")] == ["First", "Last"]  # a block too big is skipped
    assert len(parsed) == 4 and sum(parsed) <= MAX_DATA


def test_deeply_nested_event_data_is_skipped():
    page = ld(show()) + '<script type="application/ld+json">' + "[" * 100_000 + "]" * 100_000 + "</script>"
    assert [e.title for e in events_in(page, "https://x.example/", "")] == ["The Seagull"]


# --- prices ---

@pytest.mark.parametrize("price, cost", [("35", "$35"), ("$1,250.50", "$1250.50"), ("0", "Free"),
                                         ("1e999999999", None), ("NaN", None), ("Infinity", None), ("-5", None),
                                         ("1e-9", None), ("12" * 1000, None)])
def test_only_ordinary_prices_are_read(price, cost):
    started = time.monotonic()
    [e] = events_in(ld(show(offers={"price": price})), "https://x.example/", "")
    assert e.cost_text == cost
    assert time.monotonic() - started < 1


# --- downloading ---

@pytest.mark.parametrize("named, codec", [(None, "utf-8"), ("UTF-8", "utf-8"), ("latin-1", "iso8859-1"),
                                          ("windows-1252", "cp1252"), ("us-ascii", "ascii"), ("punycode", "utf-8"),
                                          ("rot13", "utf-8"), ("idna", "utf-8"), ("no-such-charset", "utf-8")])
def test_only_known_charsets_are_used(named, codec):
    assert web.charset_of(named) == codec


class _Slow(BaseHTTPRequestHandler):
    """robots.txt: none; /drip: a byte every 0.1 s; /big: 5,000 bytes."""
    def do_GET(self):
        if self.path == "/robots.txt":
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=punycode")
        self.end_headers()
        if self.path == "/big":
            self.wfile.write(b"a" * 5000); return
        try:
            for _ in range(200):
                self.wfile.write(b"a"); self.wfile.flush(); time.sleep(0.1)
        except OSError:
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def slow_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    web._robots.clear()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    web._robots.clear()


def test_a_slow_page_stops_at_the_deadline(slow_site):
    started = time.monotonic()
    with pytest.raises(web.FetchError, match="too long"):
        web.fetch(slow_site + "/drip", ai=False, timeout=5, deadline=started + 1, wait=False)
    assert time.monotonic() - started < 2


def test_a_large_page_stops_at_the_size_cap(slow_site):
    with pytest.raises(web.FetchError, match="larger than"):
        web.fetch(slow_site + "/big", ai=False, max_bytes=1000, wait=False)
    assert web.fetch(slow_site + "/big", ai=False, wait=False).text == "a" * 5000  # an unknown charset is read as utf-8


def test_a_members_link_skips_the_wait_between_requests(slow_site, monkeypatch):
    monkeypatch.setitem(web._last_hit, slow_site.removeprefix("http://"), time.monotonic())
    monkeypatch.setitem(web._robots, slow_site, web.Robots([], [], 30.0, False))  # Crawl-delay: 30
    started = time.monotonic()
    web.fetch(slow_site + "/big", ai=False, wait=False)
    assert time.monotonic() - started < 2


# --- the read's own process ---

@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setattr(fromlink, "DEADLINE", 2)


@pytest.mark.parametrize("work", [link_workers.spin, link_workers.sleep])
def test_a_read_that_runs_too_long_is_stopped(work, short_deadline):
    started = time.monotonic()
    assert fromlink.bounded_read("https://x.example/", work=work) == ("error", "reading the page took over 2 seconds")
    assert time.monotonic() - started < 4
    assert not [c for c in __import__("multiprocessing").active_children() if c.is_alive()]


def test_a_reader_that_dies_is_an_error():
    assert fromlink.bounded_read("https://x.example/", work=link_workers.die) == ("error", "the page reader stopped")


def test_the_reply_comes_back():
    assert fromlink.bounded_read("https://x.example/a", work=link_workers.echo) == ("ok", {"url": "https://x.example/a"})


def test_large_hostile_pages_are_read_within_the_deadline():
    why, got = fromlink.bounded_read("https://x.example/", work=link_workers.hostile)
    assert why == "ok" and got["seconds"] < fromlink.DEADLINE


def test_too_many_reads_at_once_are_turned_away(monkeypatch):
    monkeypatch.setattr(fromlink, "READERS", threading.BoundedSemaphore(1))
    fromlink.READERS.acquire()
    assert fromlink.bounded_read("https://x.example/", work=link_workers.echo)[0] == "busy"
    fromlink.READERS.release()
    assert fromlink.bounded_read("https://x.example/", work=link_workers.echo)[0] == "ok"


def test_the_api_answers_while_a_link_is_read(client, user_token, monkeypatch, short_deadline):
    monkeypatch.setattr(fromlink, "_addresses", lambda host, port: ["93.184.215.14"])
    monkeypatch.setattr(fromlink, "bounded_read", partial(fromlink.bounded_read, work=link_workers.spin))
    got = {}
    reading = threading.Thread(target=lambda: got.update(r=client.post("/events/from-link", json={"url": "https://x.example/"},
                                                                      headers=auth(user_token))))
    reading.start()
    time.sleep(0.5)
    started = time.monotonic()
    for _ in range(5):
        assert client.get("/health").status_code == 200
    assert time.monotonic() - started < 1 and reading.is_alive()
    reading.join()
    assert got["r"].status_code == 422 and got["r"].json()["detail"] == fromlink.WHY["error"]


# --- the address checks still hold in the reader's process ---

@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/",
                                 "http://[::1]/", "http://10.0.0.5/"])
def test_the_reader_process_refuses_private_addresses(url):
    assert fromlink.bounded_read(url)[0] == "private"


def test_a_redirect_to_a_private_address_is_refused_in_the_reader(web_pages, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fromlink, "_addresses", lambda host, port: ["127.0.0.1"] if host == "inside.example" else ["93.184.215.14"])
    web_pages["https://x.example/go"] = fromlink.PrivateAddress("redirected")
    assert fromlink.fetch_and_read("https://inside.example/")[0] == "private"


# --- reader errors are a page we couldn't read ---

def test_a_page_that_breaks_the_reader_is_unreadable(client, user_token, web_pages, monkeypatch):  # noqa: F811
    web_pages["https://x.example/show"] = LD_PAGE

    def broken(*args):
        raise LookupError("unknown encoding")
    monkeypatch.setattr(fromlink, "events_in", broken)
    r = client.post("/events/from-link", json={"url": "https://x.example/show"}, headers=auth(user_token))
    assert r.status_code == 422 and r.json()["detail"] == fromlink.WHY["error"]


def test_odd_prices_and_nesting_still_give_a_draft(client, user_token, web_pages):  # noqa: F811
    page = LD_PAGE.replace('"price": "35"', '"price": "NaN"').replace('"price": "90"', '"price": "1e999999999"')
    web_pages["https://x.example/show"] = page + '<script type="application/ld+json">' + "[" * 50_000 + "</script>"
    r = client.post("/events/from-link", json={"url": "https://x.example/show"}, headers=auth(user_token))
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "The Seagull" and "price" in r.json()["missing"]
