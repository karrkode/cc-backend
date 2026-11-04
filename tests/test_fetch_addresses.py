"""CC-189: every page fetched, whoever asked for it, is on a public address: member links, admin sources and event
pages found by discovery, redirects included, and the connection goes to the address that was checked."""
import socket
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app import fromlink
from app.discovery import fetch as web
from app.discovery import run
from app.discovery.readers import FoundEvent
from app.discovery.store import on_source_site, record
from app.models import DiscoveredItem, DiscoverySource, SourceKind
from app.stats import today_in_chicago

from .fake_net import PUBLIC, route

SITE = "http://venue.example"
TODAY = today_in_chicago()
EVENT = '{"@type": "TheaterEvent", "name": "The Seagull", "startDate": "%s", "endDate": "%s", "url": "/seagull", ' \
        '"offers": {"price": "20"}}' % (TODAY + timedelta(days=30), TODAY + timedelta(days=40))
PAGE = f'<html><script type="application/ld+json">{EVENT}</script></html>'.encode()


class _Site(BaseHTTPRequestHandler):
    """robots.txt: none; /to?<address>: a redirect there; anything else: a page."""
    hosts: list[str] = []

    def do_GET(self):
        _Site.hosts.append(self.headers["Host"])
        if self.path == "/robots.txt":
            self.send_error(404)
        elif self.path.startswith("/to?"):
            self.send_response(302); self.send_header("Location", self.path[4:]); self.end_headers()
        else:
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    web._robots.clear(); web._last_hit.clear(); _Site.hosts.clear()
    yield server.server_port
    server.shutdown()
    web._robots.clear(); web._last_hit.clear()


def make_source(db, pages) -> DiscoverySource:
    s = DiscoverySource(name="Venue", kind=SourceKind.theater, pages=pages, reader="claude", venue_name="Venue",
                        neighborhood="Loop", every_days=30, page_state={}, venue_hours={}, unchanged_streak=0,
                        last_status="", last_error="")
    db.add(s); db.commit()
    return s


def members_link(db, url) -> str:
    return fromlink.fetch_and_read(url)[0]


def admin_source(db, url) -> str:
    return run.check_source(db, make_source(db, [url]), None, TODAY).status


def event_page(db, url) -> str:
    source = make_source(db, [SITE + "/season"])
    item = SimpleNamespace(source_id=source.id, url=url, title="The Seagull")
    return run.read_event_page(db, None, item, TODAY, "a look")[1].status


PATHS = [(members_link, "private"), (admin_source, "blocked"), (event_page, "blocked")]
each_path = pytest.mark.parametrize("read,refused", PATHS, ids=["member", "source", "event"])


def nothing_private_dialed(net):
    assert {ip for ip, _ in net.dialed} <= {PUBLIC}


@each_path
@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.20", "169.254.169.254",
                                     "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "ff02::1",
                                     "::ffff:10.0.0.1"])
def test_internal_addresses_are_refused(db, site, monkeypatch, read, refused, address):
    net = route(monkeypatch, site, {"venue.example": [[address]]})
    assert read(db, SITE + "/show") == refused
    assert net.dialed == []


@each_path
@pytest.mark.parametrize("address", ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b:1::1", "::7f00:1", "::a00:5",
                                     "::ffff:0:7f00:1", "2002:7f00:1::", "2001:0:4136:e378:8000:63bf:3fff:fdd2"])
def test_ipv6_transition_ranges_are_refused(db, site, monkeypatch, read, refused, address):
    net = route(monkeypatch, site, {"venue.example": [[address]]})
    assert read(db, SITE + "/show") == refused
    assert net.dialed == []


@each_path
def test_one_private_answer_among_public_ones_is_refused(db, site, monkeypatch, read, refused):
    net = route(monkeypatch, site, {"venue.example": [[PUBLIC, "10.0.0.5"]]})
    assert read(db, SITE + "/show") == refused
    assert net.dialed == []


@each_path
@pytest.mark.parametrize("target", ["http://inside.example/", "http://127.0.0.1/", "http://[::1]/",
                                    "http://10.0.0.5/latest", "http://public.example:8000/", "ftp://public.example/x",
                                    "http://user:pw@public.example/"])
def test_a_redirect_to_a_private_address_is_refused(db, site, monkeypatch, read, refused, target):
    net = route(monkeypatch, site, {"inside.example": [["10.0.0.5"]], "127.0.0.1": [["127.0.0.1"]],
                                    "::1": [["::1"]], "10.0.0.5": [["10.0.0.5"]]})
    assert read(db, f"{SITE}/to?{target}") == refused
    nothing_private_dialed(net)
    assert all(host == "venue.example" for host in _Site.hosts)


@each_path
def test_a_redirect_to_a_file_isnt_followed(db, site, monkeypatch, read, refused):
    net = route(monkeypatch, site)
    assert read(db, f"{SITE}/to?file:///etc/passwd") == "error"  # urllib turns these down itself, as a failed request
    nothing_private_dialed(net)


@each_path
def test_a_redirect_two_hops_on_is_checked_too(db, site, monkeypatch, read, refused):
    net = route(monkeypatch, site, {"inside.example": [["10.0.0.5"]]})
    assert read(db, f"{SITE}/to?http://next.example/to?http://inside.example/") == refused
    nothing_private_dialed(net)


@each_path
def test_a_second_dns_answer_is_refused(db, site, monkeypatch, read, refused):
    """Public for robots.txt, private for the page itself."""
    net = route(monkeypatch, site, {"venue.example": [[PUBLIC], ["127.0.0.1"]]})
    assert read(db, SITE + "/show") == refused
    assert [ip for ip, _ in net.dialed] == [PUBLIC]


def test_the_early_check_on_a_members_link_decides_nothing_about_the_connection(site, monkeypatch):
    net = route(monkeypatch, site, {"venue.example": [[PUBLIC], ["127.0.0.1"]]})
    fromlink.check_public(SITE + "/show")  # read_link's first look: public
    with pytest.raises(fromlink.PrivateAddress):
        web.fetch(SITE + "/show", wait=False)
    assert net.dialed == []


def test_each_connection_uses_the_answer_it_checked(site, monkeypatch):
    net = route(monkeypatch, site, {"venue.example": [["93.184.215.14"], ["93.184.215.15"]]})
    page = web.fetch(SITE + "/show", wait=False)
    assert page.status == 200 and page.text == PAGE.decode()
    assert net.lookups == ["venue.example"] * 2  # one lookup for each of robots.txt and the page, and no more
    assert net.dialed == [("93.184.215.14", 80), ("93.184.215.15", 80)]
    assert _Site.hosts == ["venue.example"] * 2  # the Host header keeps the name


def test_the_next_answer_is_tried_when_one_is_down(site, monkeypatch):
    net = route(monkeypatch, site, {"venue.example": [["2606:2800:21f:cb07:6820:80da:af6b:8b2c", PUBLIC]]})
    real = net.dial

    def v6_down(address, timeout=None, source_address=None):
        if ":" in address[0]:
            net.dialed.append(address)
            raise OSError("network unreachable")
        return real(address, timeout, source_address)
    monkeypatch.setattr(socket, "create_connection", v6_down)
    assert web.fetch(SITE + "/show", wait=False).status == 200


def test_a_redirect_to_another_public_site_is_followed(site, monkeypatch):
    route(monkeypatch, site)
    page = web.fetch(f"{SITE}/to?http://moved.example/show", wait=False)
    assert page.url == "http://moved.example/show" and page.status == 200


def test_https_keeps_the_hostname_for_sni(monkeypatch):
    route(monkeypatch, 0)
    monkeypatch.setattr(socket, "create_connection", lambda *a: SimpleNamespace(setsockopt=lambda *a: None))
    named = []
    tls = SimpleNamespace(wrap_socket=lambda sock, server_hostname: named.append(server_hostname) or sock)
    web._PublicHTTPS("venue.example", 443, context=tls).connect()
    assert named == ["venue.example"]


def test_a_proxy_setting_doesnt_get_around_the_check(site, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://10.0.0.5:3128")
    net = route(monkeypatch, site)
    assert web.fetch(SITE + "/show", wait=False).status == 200
    assert {ip for ip, _ in net.dialed} == {PUBLIC}


def test_a_host_that_doesnt_exist_is_a_fetch_error(site, monkeypatch):
    def nothing(host, port):
        raise socket.gaierror("no such host")
    route(monkeypatch, site)
    monkeypatch.setattr(web, "_addresses", nothing)
    with pytest.raises(web.FetchError, match="no website at venue.example"):
        web.fetch(SITE + "/show", wait=False)


@pytest.mark.parametrize("address", ["93.184.215.14", "8.8.8.8", "2606:2800:21f:cb07:6820:80da:af6b:8b2c",
                                     "::ffff:93.184.215.14"])
def test_public_addresses_are_public(address):
    assert web.is_public(address)


# --- what's stored ---

def found(url) -> FoundEvent:
    return FoundEvent(title="The Seagull", url=url, kind="production")


@pytest.mark.parametrize("url,kept", [
    ("https://www.venue.example/shows/seagull", True), ("http://venue.example/seagull", True),
    ("https://tickets.venue.example/seagull", True), ("HTTPS://WWW.VENUE.EXAMPLE/seagull", True),
    ("https://elsewhere.example/seagull", False), ("https://venue.example.elsewhere.example/seagull", False),
    ("http://127.0.0.1:8000/openapi.json", False), ("javascript:alert(1)", False), ("data:text/html,x", False),
    ("ftp://venue.example/seagull", False), ("/seagull", False), ("http://[bad/", False), ("", False), (None, False)])
def test_only_http_links_on_the_sources_site_are_stored(db, url, kept):
    source = make_source(db, ["https://www.venue.example/season"])
    assert on_source_site(url, source) is kept
    record(db, source, found(url), "")
    db.commit()
    item = db.query(DiscoveredItem).one()
    assert item.title == "The Seagull" and item.url == (url if kept else "")
    assert item.key.startswith("title:") is not kept


def test_any_of_a_sources_pages_gives_its_site(db):
    source = make_source(db, ["https://www.venue.example/season", "https://api.othername.example/v1"])
    assert on_source_site("https://www.othername.example/x", source)


def test_a_link_left_out_keeps_the_one_already_stored(db):
    source = make_source(db, ["https://venue.example/season"])
    record(db, source, found("https://venue.example/seagull"), "")
    record(db, source, found("https://elsewhere.example/seagull"), "")
    db.commit()
    assert [i.url for i in db.query(DiscoveredItem)] == ["https://venue.example/seagull"]


def test_a_stored_link_off_the_sources_site_isnt_fetched(db, monkeypatch):
    source = make_source(db, ["https://venue.example/season"])
    monkeypatch.setattr(web, "fetch", lambda *a, **k: pytest.fail("fetched a link off the source's site"))
    item = SimpleNamespace(source_id=source.id, url="https://elsewhere.example/seagull", title="The Seagull")
    assert run.read_event_page(db, None, item, TODAY, "a look")[1].status == "blocked"
