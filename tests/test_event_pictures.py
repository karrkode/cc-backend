"""CC-246: a sourced event's picture comes from its own page, is fetched safely and copied into our storage, and a
page or picture that can't be used never holds the event up."""
import io
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from app import event_images
from app.config import get_settings
from app.discovery import fetch as web
from app.discovery import pictures, run
from app.models import DiscoverySource, EventStatus, SourceKind
from app.stats import today_in_chicago

from .fake_net import PUBLIC, route

SITE = "http://venue.example"


def _image(fmt="JPEG", size=(1600, 900), colour=(200, 60, 40)):
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, fmt)
    return out.getvalue()


def _page(*heads: str) -> bytes:
    return f"<html><head>{''.join(heads)}</head><body>The Seagull</body></html>".encode()


class _Site(BaseHTTPRequestHandler):
    """Serves ROUTES: path -> (status, content type, body). robots.txt is a 404 (no rules) unless given."""
    ROUTES: dict[str, tuple[int, str, bytes]] = {}
    asked: list[str] = []

    def do_GET(self):
        _Site.asked.append(self.headers["Host"] + self.path)
        status, ctype, body = _Site.ROUTES.get(self.path, (404, "text/html", b""))
        self.send_response(status)
        if status in (301, 302):  # the body is where it goes
            self.send_header("Location", body.decode()); body = b""
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def site(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    web._robots.clear(); web._last_hit.clear(); _Site.asked.clear()
    _Site.ROUTES = {"/seagull": (200, "text/html", _page('<meta property="og:image" content="/art/seagull.jpg">')),
                    "/art/seagull.jpg": (200, "image/jpeg", _image())}
    monkeypatch.setattr(web, "MIN_GAP_SECONDS", 0)
    net = route(monkeypatch, server.server_port)
    yield net
    server.shutdown()
    web._robots.clear(); web._last_hit.clear()


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_IMAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def sourced(db, make_event):
    """An approved event found by discovery, as approving a found event makes it."""
    source = DiscoverySource(name="Venue", kind=SourceKind.theater, pages=[SITE + "/season"], reader="claude",
                             venue_name="Venue", neighborhood="Loop", every_days=30, page_state={}, venue_hours={},
                             unchanged_streak=0, last_status="", last_error="")
    db.add(source); db.flush()

    def _make(title="The Seagull", url=SITE + "/seagull", **kw):
        ev = make_event(title=title, **kw)
        ev.found_by_id, ev.url = source.id, url
        db.commit()
        return ev
    return _make


# --- reading the page ---

def test_the_preview_picture_is_read_best_first_and_made_absolute():
    html = ('<meta name="twitter:image" content="https://cdn.example/tw.jpg">'
            '<meta content="/og.jpg?w=1200&amp;h=630" property="og:image">'
            '<link rel="image_src" href="//cdn.example/link.jpg">'
            '<script type="application/ld+json">{"@type": "TheaterEvent", "name": "X", '
            '"image": [{"@type": "ImageObject", "url": "ld.jpg"}]}</script>')
    assert pictures.preview_images(html, "https://venue.example/shows/x") == [
        "https://venue.example/og.jpg?w=1200&h=630", "https://cdn.example/tw.jpg", "https://cdn.example/link.jpg",
        "https://venue.example/shows/ld.jpg"]


@pytest.mark.parametrize("content", ["javascript:alert(1)", "data:image/png;base64,AAAA", "ftp://venue.example/a.jpg",
                                     "file:///etc/passwd", "http://[bad/", ""])
def test_only_web_addresses_are_taken(content):
    assert pictures.preview_images(f'<meta property="og:image" content="{content}">', "https://venue.example/") == []


def test_a_page_without_one_names_none():
    assert pictures.preview_images("<html><title>Hi</title><meta name=description content=x></html>", SITE) == []


@pytest.mark.parametrize("ctype,data,kind", [
    ("image/jpeg", _image("JPEG"), "image/jpeg"), ("image/png; charset=x", _image("PNG"), "image/png"),
    ("image/webp", _image("WEBP"), "image/webp"), ("image/gif", _image("GIF"), "image/gif"),
    ("image/avif", _image("AVIF"), "image/avif"),
    ("image/jpeg", b"<html>not a picture</html>", None),  # the header alone isn't enough
    ("image/png", _image("JPEG"), None),  # the bytes have to agree with it
    ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>", None),  # scripts can hide in SVG
    ("text/html", _image("JPEG"), None), ("", _image("JPEG"), None)])
def test_only_safe_types_whose_bytes_agree_are_taken(ctype, data, kind):
    assert pictures.kind_of(data, ctype) == kind


def test_a_picture_with_too_many_pixels_is_refused_before_it_is_unpacked(monkeypatch):
    monkeypatch.setattr(event_images, "MAX_PIXELS", 1000)
    with pytest.raises(event_images.BadImage, match="too many pixels"):
        event_images.normalise(_image(size=(100, 100)))


# --- storing it ---

def test_a_new_sourced_event_gets_its_picture_copied_into_our_storage(db, site, stored, sourced):
    ev = sourced()
    [(name, status, note)] = pictures.add_pictures(db)
    assert (name, status) == ("Venue", "ok") and "picture stored from venue.example" in note
    db.refresh(ev)
    assert ev.image_key.startswith(f"event-images/{ev.id}/") and ev.image_key.endswith(".webp")
    assert ev.image_url == f"{get_settings().site_url.rstrip('/')}/{ev.image_key}"  # ours, never the venue's address
    assert ev.image_credit_url == SITE + "/seagull"
    webp = (stored / ev.image_key).read_bytes()
    assert Image.open(io.BytesIO(webp)).format == "WEBP"
    assert (stored / "originals" / str(ev.id)).is_dir()  # the original is kept, privately
    assert pictures.due(db, datetime.now(UTC)) == []


def test_the_event_page_api_shows_the_picture_and_its_credit(db, client, site, stored, sourced):
    ev = sourced()
    pictures.add_pictures(db)
    got = client.get(f"/events/{ev.id}").json()
    assert got["image_url"].endswith(".webp") and got["image_credit_url"] == SITE + "/seagull"


def test_no_picture_leaves_the_water_and_isnt_looked_for_again(db, client, site, stored, sourced):
    _Site.ROUTES["/seagull"] = (200, "text/html", _page())
    ev = sourced()
    [(_, status, note)] = pictures.add_pictures(db)
    assert status == "none" and "names no picture" in note
    db.refresh(ev)
    assert ev.status == EventStatus.approved and ev.image_key is None and ev.picture_tries == pictures.MAX_TRIES
    got = client.get(f"/events/{ev.id}").json()
    assert got["image_url"] is None and got["image_credit_url"] is None
    later = datetime.now(UTC) + timedelta(days=30)
    assert pictures.due(db, later) == []


def test_a_page_that_cant_be_fetched_is_tried_again_tomorrow_three_times_at_most(db, site, stored, sourced):
    _Site.ROUTES["/seagull"] = (503, "text/html", b"busy")
    ev = sourced()
    now = datetime.now(UTC)
    assert [s for _, s, _ in pictures.add_pictures(db, now)] == ["error"]
    assert pictures.due(db, now + timedelta(hours=1)) == []  # not again the next hour
    assert [s for _, s, _ in pictures.add_pictures(db, now + timedelta(days=1))] == ["error"]
    assert [s for _, s, _ in pictures.add_pictures(db, now + timedelta(days=2))] == ["error"]
    db.refresh(ev)
    assert ev.picture_tries == pictures.MAX_TRIES and pictures.due(db, now + timedelta(days=9)) == []


def test_it_works_on_a_later_try(db, site, stored, sourced):
    _Site.ROUTES["/seagull"] = (503, "text/html", b"busy")
    ev = sourced()
    now = datetime.now(UTC)
    pictures.add_pictures(db, now)
    _Site.ROUTES["/seagull"] = (200, "text/html", _page('<meta property="og:image" content="/art/seagull.jpg">'))
    assert [s for _, s, _ in pictures.add_pictures(db, now + timedelta(days=1))] == ["ok"]
    db.refresh(ev)
    assert ev.image_key and ev.picture_tries == 2


@pytest.mark.parametrize("route_,why", [
    ((200, "image/jpeg", b"<html>no</html>"), "not a picture we take"),
    ((200, "image/svg+xml", b"<svg/>"), "not a picture we take"),
    ((200, "image/jpeg", _image(size=(64, 64))), "too small"),
    ((200, "image/jpeg", b"\xff\xd8\xff" + b"0" * 500), "can't be read"),
    ((403, "text/html", b""), "refused"),
])
def test_an_unusable_picture_is_never_stored(db, site, stored, sourced, route_, why):
    _Site.ROUTES["/art/seagull.jpg"] = route_
    ev = sourced()
    [(_, status, note)] = pictures.add_pictures(db)
    assert status == "none" and why in note
    db.refresh(ev)
    assert ev.image_key is None and not any(stored.iterdir())


def test_a_picture_over_the_size_cap_isnt_downloaded_in_full(db, site, stored, sourced, monkeypatch):
    monkeypatch.setattr(pictures, "MAX_BYTES", 10_000)
    ev = sourced()
    [(_, status, note)] = pictures.add_pictures(db)
    assert status == "error" and "larger than" in note  # the cap, then tried again tomorrow like any failed fetch
    db.refresh(ev)
    assert ev.image_key is None and not any(stored.iterdir())


def test_a_broken_first_picture_falls_back_to_the_next(db, site, stored, sourced):
    _Site.ROUTES["/seagull"] = (200, "text/html", _page('<meta property="og:image" content="/gone.jpg">',
                                                        '<meta name="twitter:image" content="/art/seagull.jpg">'))
    ev = sourced()
    assert [s for _, s, _ in pictures.add_pictures(db)] == ["ok"]


def test_the_sites_logo_on_every_page_isnt_taken_for_the_event(db, site, stored, sourced):
    first, second = sourced(), sourced(title="Uncle Vanya", url=SITE + "/vanya")
    _Site.ROUTES["/vanya"] = _Site.ROUTES["/seagull"]  # both pages name the same share image
    assert [s for _, s, _ in pictures.add_pictures(db)] == ["ok", "none"]
    db.refresh(second)
    assert second.image_key is None


def test_a_picture_on_a_private_address_is_refused_unfetched(db, site, stored, sourced):
    _Site.ROUTES["/seagull"] = (200, "text/html", _page('<meta property="og:image" content="http://inside.example/a.jpg">'))
    site.answers["inside.example"] = [["10.0.0.5"]]
    ev = sourced()
    [(_, status, note)] = pictures.add_pictures(db)
    assert status == "none" and "private" in note
    assert {ip for ip, _ in site.dialed} == {PUBLIC} and not any("inside" in a for a in _Site.asked)


def test_a_redirect_to_a_private_address_is_refused(db, site, stored, sourced):
    _Site.ROUTES["/art/seagull.jpg"] = (302, "text/html", b"http://inside.example/a.jpg")
    site.answers["inside.example"] = [["127.0.0.1"]]
    ev = sourced()
    [(_, status, note)] = pictures.add_pictures(db)
    assert status == "none" and "private" in note
    assert {ip for ip, _ in site.dialed} == {PUBLIC} and not any("inside" in a for a in _Site.asked)


def test_robots_txt_is_followed_for_pictures(db, site, stored, sourced):
    _Site.ROUTES["/robots.txt"] = (200, "text/plain", b"User-agent: *\nDisallow: /art/\n")
    ev = sourced()
    [(_, status, note)] = pictures.add_pictures(db)
    assert status == "none" and "robots.txt" in note
    assert not any(a.endswith("/art/seagull.jpg") for a in _Site.asked)


def test_an_ai_opt_out_doesnt_stop_a_picture(db, site, stored, sourced):
    """Nothing about the picture goes to Claude, so only the rules for every crawler apply."""
    _Site.ROUTES["/robots.txt"] = (200, "text/plain", b"User-agent: ClaudeBot\nDisallow: /\n")
    sourced()
    assert [s for _, s, _ in pictures.add_pictures(db)] == ["ok"]


def test_only_live_sourced_events_without_a_photo_are_looked_for(db, site, stored, sourced, make_event):
    make_event(title="Typed in by an admin")  # not sourced
    sourced(title="Waiting", status=EventStatus.pending)
    has_one = sourced(title="Has one")
    has_one.image_key = "event-images/1/abc.webp"
    no_link = sourced(title="No link", url="")
    looked = sourced(title="Looked at by CC-250")
    looked.picture_tries = pictures.MAX_TRIES
    db.commit()
    assert pictures.due(db, datetime.now(UTC)) == []
    assert no_link.image_key is None


def test_the_hourly_run_looks_for_pictures_and_a_failure_stops_nothing(db, site, stored, sourced, monkeypatch):
    sourced()
    sourced(title="Uncle Vanya", url=SITE + "/vanya")  # a 404: tried again tomorrow
    results = [(n, o.status) for n, o in run.run(db, None, today_in_chicago()) if n.endswith("(picture)")]
    assert results == [("Venue (picture)", "ok"), ("Venue (picture)", "error")]


def test_a_storage_failure_is_tried_again(db, site, sourced, monkeypatch):
    class Down:
        def exists(self, key): return False
        def put(self, *a): raise OSError("storage is down")
    ev = sourced()
    [(_, status, note)] = pictures.add_pictures(db, to=Down())
    assert status == "error" and "storage is down" in note
    db.refresh(ev)
    assert ev.image_key is None and ev.picture_tries == 1
