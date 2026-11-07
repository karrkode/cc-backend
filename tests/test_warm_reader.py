"""CC-216: the page reader is ready before the first link, and a site's robots.txt is read once every few minutes,
while every fetch and redirect is still checked."""
import subprocess
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import fromlink
from app.discovery import fetch as web
from app.main import app

from . import link_workers

SITE = "http://site.example"
PAGE = '<script type="application/ld+json">{"@type": "TheaterEvent", "name": "The Seagull", "startDate": "2030-01-05"}</script>'


# --- the warm start ---

def test_the_api_starts_the_reader(monkeypatch):
    started = []
    monkeypatch.setattr(fromlink.forkserver, "ensure_running", lambda: started.append(True))
    with TestClient(app):
        assert started == [True]


def test_warming_starts_the_server_with_the_reader_loaded():
    code = ("from multiprocessing import forkserver\n"
            "from app import fromlink\n"
            "assert forkserver._forkserver._forkserver_pid is None\n"
            "fromlink.warm_reader()\n"
            "assert forkserver._forkserver._forkserver_pid is not None\n"
            "assert forkserver._forkserver._preload_modules == ['app.fromlink', 'app.venuelink']\n")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


def test_a_failed_warm_start_doesnt_stop_the_api(monkeypatch):
    def fails():
        raise OSError("no more processes")
    monkeypatch.setattr(fromlink.forkserver, "ensure_running", fails)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


# --- reusing robots.txt, through the reader's real process ---

class _Site(BaseHTTPRequestHandler):
    robots, robots_status, hits = "User-agent: *\nDisallow: /private\n", 200, []

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path == "/robots.txt":
            self.send_response(self.robots_status)
            self.end_headers()
            self.wfile.write(self.robots.encode())
        elif self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", "http://inside.example/")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def read(monkeypatch):
    """Reads a link in its own process, on a fake internet that lands on the test site."""
    monkeypatch.setattr(_Site, "hits", [])
    monkeypatch.setattr(fromlink, "_recent_robots", {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def reader(url, answers=None):
        return fromlink.bounded_read(url, work=partial(link_workers.routed, server.server_port, answers or {}))
    yield reader
    server.shutdown()


def robots_reads() -> int:
    return _Site.hits.count("/robots.txt")


def later(monkeypatch, seconds: float) -> None:
    now = fromlink.monotonic()
    monkeypatch.setattr(fromlink, "monotonic", lambda: now + seconds)


def test_a_second_link_to_the_site_skips_robots_txt(read):
    assert read(SITE + "/a")[0] == "ok"
    assert read(SITE + "/b")[0] == "ok"
    assert _Site.hits == ["/robots.txt", "/a", "/b"]


def test_robots_txt_is_read_again_after_the_window(read, monkeypatch):
    read(SITE + "/a")
    later(monkeypatch, fromlink.ROBOTS_KEEP - 1)
    read(SITE + "/b")
    assert robots_reads() == 1
    later(monkeypatch, fromlink.ROBOTS_KEEP + 1)
    read(SITE + "/c")
    assert robots_reads() == 2


def test_using_the_rules_doesnt_extend_the_window(read, monkeypatch):
    read(SITE + "/a")
    later(monkeypatch, fromlink.ROBOTS_KEEP - 1)
    read(SITE + "/b")  # reuses them: they're still as old as the first read
    later(monkeypatch, fromlink.ROBOTS_KEEP + 1)
    read(SITE + "/c")
    assert robots_reads() == 2


def test_another_site_has_its_own_rules(read):
    read(SITE + "/a")
    read("http://other.example/a")
    assert robots_reads() == 2


def test_the_rules_are_shared_across_a_sites_forms_but_never_applied_across_origins(read):
    read("http://www.site.example/a")
    assert list(fromlink._robots_known("https://site.example/")) == ["http://www.site.example"]
    read("http://site.example/a")  # an origin of its own, so robots.txt is still read for it
    assert robots_reads() == 2


def test_reused_rules_still_apply(read, monkeypatch):
    monkeypatch.setattr(_Site, "robots", "User-agent: *\nDisallow: /private\n\nUser-agent: ClaudeBot\nDisallow: /\n")
    assert read(SITE + "/a")[1]["ai_opt_out"] is True
    assert read(SITE + "/b")[1]["ai_opt_out"] is True
    assert read(SITE + "/private")[0] == "blocked"
    assert "/private" not in _Site.hits and robots_reads() == 1


def test_no_robots_txt_is_remembered_too(read, monkeypatch):
    monkeypatch.setattr(_Site, "robots_status", 404)
    read(SITE + "/a")
    read(SITE + "/b")
    assert robots_reads() == 1


def test_a_failed_page_keeps_the_rules_it_read(read):
    assert read(SITE + "/private")[0] == "blocked"
    read(SITE + "/a")
    assert robots_reads() == 1


@pytest.mark.parametrize("code, why", [(500, "ok"), (503, "ok"), (429, "ok"), (403, "blocked")])
def test_an_unclear_answer_isnt_kept(read, monkeypatch, code, why):
    monkeypatch.setattr(_Site, "robots_status", code)
    assert read(SITE + "/a")[0] == why
    monkeypatch.setattr(_Site, "robots_status", 200)
    read(SITE + "/b")
    assert robots_reads() == 2 and fromlink._robots_known(SITE + "/")[SITE].disallow == ["/private"]


# --- the address checks don't depend on robots.txt being read ---

def test_a_host_that_turns_private_is_refused_with_its_rules_reused(read):
    assert read(SITE + "/a")[0] == "ok"
    assert read(SITE + "/b", {"site.example": [["127.0.0.1"]]})[0] == "private"
    assert _Site.hits == ["/robots.txt", "/a"]


def test_a_redirect_to_a_private_address_is_refused_with_the_rules_reused(read):
    answers = {"inside.example": [["127.0.0.1"]]}
    assert read(SITE + "/a", answers)[0] == "ok"
    assert read(SITE + "/hop", answers)[0] == "private"
    assert _Site.hits == ["/robots.txt", "/a", "/hop"]


# --- what's kept stays small ---

def rules(n: int = 0) -> web.Robots:
    return web.Robots(["/x"] * n, [], 0.0, False)


def test_only_the_latest_sites_are_kept(monkeypatch):
    monkeypatch.setattr(fromlink, "_recent_robots", {})
    for i in range(fromlink.ROBOTS_SITES + 5):
        fromlink._keep_robots({f"https://site{i}.example": rules()})
    assert len(fromlink._recent_robots) == fromlink.ROBOTS_SITES
    assert "https://site0.example" not in fromlink._recent_robots
    assert f"https://site{fromlink.ROBOTS_SITES + 4}.example" in fromlink._recent_robots


def test_a_huge_robots_txt_isnt_kept(monkeypatch):
    monkeypatch.setattr(fromlink, "_recent_robots", {})
    fromlink._keep_robots({"https://big.example": rules(fromlink.ROBOTS_RULES + 1)})
    assert fromlink._recent_robots == {}


def test_expired_rules_are_dropped(monkeypatch):
    monkeypatch.setattr(fromlink, "_recent_robots", {})
    fromlink._keep_robots({"https://old.example": rules()})
    later(monkeypatch, fromlink.ROBOTS_KEEP + 1)
    fromlink._keep_robots({"https://new.example": rules()})
    assert list(fromlink._recent_robots) == ["https://new.example"]
