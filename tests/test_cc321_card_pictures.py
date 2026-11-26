"""CC-321: pictures from the listing card and from the event page's main picture, when the preview tags give none.
Hand-made pages and a fake net: nothing here touches a live site."""
from datetime import UTC, datetime

from PIL import Image
import io

from app.discovery import htmlpics, pictures
from app.models import DiscoveredItem

from .test_cc320_picture_refusals import (CDN_HOST, PAGE_HOST, _Hosts, _item, _jpeg, _on_venue, _page,  # noqa: F401 - fixtures
                                           hosts, stored)
from .test_event_pictures import _image

NOW = datetime(2027, 1, 5, 15, tzinfo=UTC)
LISTING = f"http://{PAGE_HOST}/season"


def _routes(hosts, listing: str, **pages):
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(PAGE_HOST, "/season")] = (200, "text/html", f"<html><body><main>{listing}</main></body></html>".encode())
    for path, body in pages.items():
        hosts[(PAGE_HOST, "/" + path)] = (200, "text/html", body.encode() if isinstance(body, str) else body)


def _card(href, title, image, extra=""):
    return f'<li class="card"><a href="{href}"><img src="{image}" alt=""></a><h3>{title}</h3>{extra}</li>'


def _ul(*cards):
    return "<ul>" + "".join(cards) + "</ul>"


def _blank_jpeg():
    out = io.BytesIO()
    Image.new("RGB", (1600, 900), (10, 10, 10)).save(out, "JPEG")
    return out.getvalue()


def _look(db, it):
    [(_, status, note)] = pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    return status, note


# --- 1, 2, 10: which card, and where the picture came from ---------------------------------------------------------------

def test_a_card_whose_link_is_the_events_gives_its_picture(db, hosts, stored):
    it = _item(db, _on_venue(db))
    _jpeg(hosts, path="/seagull.jpg")
    _routes(hosts, _ul(_card("/seagull", "Something else entirely", f"http://{CDN_HOST}/seagull.jpg")), seagull=_page())
    status, note = _look(db, it)
    assert status == "ok" and "card on the listing page" in note
    assert it.image_key and it.image_source_url == LISTING  # the page the picture came from, not the event's page


def test_a_card_whose_heading_is_the_events_title_gives_its_picture(db, hosts, stored):
    it = _item(db, _on_venue(db))
    _jpeg(hosts, path="/seagull.jpg")
    _routes(hosts, _ul(_card("/tickets/77", "The  Seagull!", f"http://{CDN_HOST}/seagull.jpg")), seagull=_page())
    assert _look(db, it)[0] == "ok" and it.image_key


def test_a_card_that_is_another_events_is_not_used(db, hosts, stored):
    it = _item(db, _on_venue(db))
    _jpeg(hosts, path="/hamlet.jpg")
    _routes(hosts, _ul(_card("/hamlet", "Hamlet", f"http://{CDN_HOST}/hamlet.jpg")), seagull=_page())
    status, _ = _look(db, it)
    assert status == "none" and it.image_key is None and it.image_note == "The page names no picture"


def test_the_preview_tags_come_first_and_the_listing_is_then_not_fetched(db, hosts, stored):
    it = _item(db, _on_venue(db))
    _jpeg(hosts, path="/og.jpg")
    _routes(hosts, "", seagull=_page(f'<meta property="og:image" content="http://{CDN_HOST}/og.jpg">'))
    status, note = _look(db, it)
    assert status == "ok" and "card" not in note and ("venue.example", "/season") not in _Hosts.asked


# --- 3: an event with no page of its own ---------------------------------------------------------------------------------

def test_an_event_with_no_address_gets_its_cards_picture(db, hosts, stored):
    it = _item(db, _on_venue(db), url="")
    _jpeg(hosts, path="/seagull.jpg")
    _routes(hosts, _ul(_card("/x", "The Seagull", f"http://{CDN_HOST}/seagull.jpg")))
    assert _look(db, it)[0] == "ok" and it.image_key and it.image_source_url == LISTING


def test_an_event_whose_address_is_the_listing_page_gets_its_cards_picture(db, hosts, stored):
    it = _item(db, _on_venue(db), url=LISTING)
    _jpeg(hosts, path="/seagull.jpg")
    _routes(hosts, _ul(_card("/x", "The Seagull", f"http://{CDN_HOST}/seagull.jpg")))
    assert _look(db, it)[0] == "ok" and it.image_key


def test_no_address_and_no_card_still_says_there_is_no_page(db, hosts, stored):
    it = _item(db, _on_venue(db), url="")
    _routes(hosts, _ul(_card("/x", "Hamlet", f"http://{CDN_HOST}/a.jpg")))
    status, _ = _look(db, it)
    assert status == "none" and "no page of its own" in it.image_note


# --- 4: the main picture --------------------------------------------------------------------------------------------------

PAGE_WITH_FURNITURE = """<html><body>
<header><img src="http://cdn.example/head.jpg" width="1200" height="400"></header>
<nav><img src="http://cdn.example/nav.jpg" width="900" height="900"></nav>
<main><img src="http://cdn.example/logo.png" width="900" height="900" alt="Venue logo">
<img src="http://cdn.example/pixel.gif" width="1" height="1">
<div class="sponsors"><img src="http://cdn.example/sponsor.jpg" width="1500" height="500"></div>
<img src="http://cdn.example/small.jpg" width="640" height="360">
<img src="http://cdn.example/poster.jpg" width="1200" height="800"></main>
<footer><img src="http://cdn.example/foot.jpg" width="2000" height="2000"></footer></body></html>"""


def test_the_main_picture_is_the_largest_in_the_main_content():
    assert htmlpics.main_picture(PAGE_WITH_FURNITURE, "http://venue.example/seagull")[0] == "http://cdn.example/poster.jpg"


def test_an_event_page_without_a_card_gives_its_main_picture(db, hosts, stored):
    it = _item(db, _on_venue(db))
    _jpeg(hosts, path="/poster.jpg")
    _routes(hosts, "", seagull=PAGE_WITH_FURNITURE)
    status, note = _look(db, it)
    assert status == "ok" and "main picture" in note and it.image_source_url == f"http://{PAGE_HOST}/seagull"


def test_furniture_alone_gives_no_picture():
    html = ('<header><img src="/h.jpg" width="900" height="900"></header><footer><img src="/f.jpg"></footer>'
            '<nav><img src="/n.jpg"></nav><img src="/logo.png"><img src="/s.svg">')
    assert htmlpics.main_picture(html, "http://venue.example/x") == []


# --- 5: a picture several events would share ------------------------------------------------------------------------------

def test_a_picture_two_titles_share_is_refused_for_both(db, hosts, stored):
    src = _on_venue(db)
    a, b = _item(db, src), _item(db, src, title="Hamlet", url=f"http://{PAGE_HOST}/hamlet")
    _jpeg(hosts, path="/same.jpg")
    _routes(hosts, _ul(_card("/seagull", "The Seagull", f"http://{CDN_HOST}/same.jpg"),
                       _card("/hamlet", "Hamlet", f"http://{CDN_HOST}/same.jpg")),
            seagull=_page(), hamlet=_page())
    rows = pictures.add_item_pictures(db, NOW)
    db.refresh(a); db.refresh(b)
    assert [r[1] for r in rows] == ["none", "none"] and a.image_key is None and b.image_key is None
    assert "shared" in a.image_note


def test_a_shared_picture_is_refused_as_a_main_picture_too(db, hosts, stored):
    it = _item(db, _on_venue(db))
    _jpeg(hosts, path="/same.jpg")
    _routes(hosts, _ul(_card("/x", "Other A", f"http://{CDN_HOST}/same.jpg"), _card("/y", "Other B", f"http://{CDN_HOST}/same.jpg")),
            seagull='<main><img src="http://cdn.example/same.jpg" width="1200" height="800"></main>')
    assert _look(db, it)[0] == "none" and it.image_key is None


# --- 6: pages that aren't there, and blank pictures -------------------------------------------------------------------------

def test_a_404_page_gives_no_picture(db, hosts, stored):
    it = _item(db, _on_venue(db))
    hosts[(PAGE_HOST, "/robots.txt")] = (404, "text/plain", b"")
    hosts[(PAGE_HOST, "/season")] = (200, "text/html", b"<main></main>")
    # /seagull is not routed: the fake site answers 404
    status, _ = _look(db, it)
    assert status == "error" and it.image_key is None and "not found" in it.image_note


def test_the_sites_own_not_found_page_gives_no_picture(db, hosts, stored):
    it = _item(db, _on_venue(db))
    _jpeg(hosts, path="/hero.jpg")
    _routes(hosts, "", seagull='<html><head><title>Page not found</title></head><body><main>'
                              '<img src="http://cdn.example/hero.jpg" width="1200" height="800"></main></body></html>')
    status, _ = _look(db, it)
    assert status == "none" and it.image_key is None and "not found" in it.image_note.lower()


def test_a_blank_picture_is_refused(db, hosts, stored):
    it = _item(db, _on_venue(db))
    hosts[(CDN_HOST, "/flat.jpg")] = (200, "image/jpeg", _blank_jpeg())
    _routes(hosts, _ul(_card("/seagull", "The Seagull", f"http://{CDN_HOST}/flat.jpg")), seagull=_page())
    status, _ = _look(db, it)
    assert status == "none" and it.image_key is None and "blank" in it.image_note


# --- 7: lazy-loaded pictures ------------------------------------------------------------------------------------------------

def test_lazy_loaded_pictures_are_read_from_the_html():
    page = ('<main><picture><source srcset="/s-400.jpg 400w, /s-1600.jpg 1600w"><img src="data:image/gif;base64,R0lGOD"></picture>'
            '<img data-src="/lazy.jpg" width="800" height="500" alt=""></main>')
    assert htmlpics.main_picture(page, "http://venue.example/x")[0] == "http://venue.example/s-1600.jpg"
    cards = htmlpics.listing_cards('<ul><li><a href="/a"><img src="data:," data-srcset="/a-300.jpg 300w, /a-900.jpg 900w"></a>'
                                   '<h3>A</h3></li></ul>', "http://venue.example/season")
    assert [c.image for c in cards] == ["http://venue.example/a-900.jpg"]


def test_noscript_copies_do_not_split_a_card():
    html = ('<ul><li><a href="/a"><img data-src="/a.jpg"><noscript><img src="/a.jpg"></noscript></a><h3>A</h3></li>'
            '<li><a href="/b"><img data-src="/b.jpg"></a><h3>B</h3></li></ul>')
    cards = htmlpics.listing_cards(html, "http://venue.example/season")
    assert [(c.who, c.image) for c in cards] == [("a", "http://venue.example/a.jpg"), ("b", "http://venue.example/b.jpg")]


# --- 8, 9: the existing rules, and one listing fetch per run ------------------------------------------------------------------

def test_a_hash_link_card_is_not_the_listing_pages_event(db, hosts, stored):
    it = _item(db, _on_venue(db), title="Mine", url=LISTING)
    _jpeg(hosts, path="/other.jpg")
    _routes(hosts, _ul(_card("#", "Other", f"http://{CDN_HOST}/other.jpg")))  # a modal card: its link is the listing itself
    status, _ = _look(db, it)
    assert status == "none" and it.image_key is None


def test_body_classes_and_self_closed_tags_do_not_hide_the_page():
    big = '<main><img src="/p.jpg" width="1200" height="800"></main>'
    for page in (f'<body class="page no-sidebar menu-fixed">{big}</body>', f'<svg class="i"/>{big}', f'<script src="/a.js"/>{big}'):
        assert htmlpics.main_picture(page, "http://venue.example/x") == ["http://venue.example/p.jpg"]


def test_only_real_logos_and_icons_are_skipped():
    ok = ('<main><img src="/poster-11x14.jpg" width="600" height="800" alt="Iconic Chicago show"></main>')
    assert htmlpics.main_picture(ok, "http://venue.example/x") == ["http://venue.example/poster-11x14.jpg"]
    for bad in ('<img src="/site-logo.png" width="900" height="900">', '<img src="/a.jpg" class="logo" width="900" height="900">',
                '<img src="/1x1.gif">', '<img src="/img/sponsors/a.jpg" width="900" height="900">'):
        assert htmlpics.main_picture(f"<main>{bad}</main>", "http://venue.example/x") == []


def test_only_a_pages_own_not_found_counts():
    for html in ("<title>Page not found | Venue</title>", "<h1>404</h1>", "<h1>404 - Page Not Found</h1>",
                 "<title>Sorry, this page can't be found</title>", "<h1>Not Found</h1>"):
        assert htmlpics.looks_missing(html), html
    for html in ("<h1>Room 404: A Comedy</h1>", "<title>Lost &amp; Not Found Improv | Venue</title>", "<h1>The Seagull</h1>"):
        assert not htmlpics.looks_missing(html), html


def test_a_404_page_does_not_borrow_its_cards_picture(db, hosts, stored):
    it = _item(db, _on_venue(db))
    _jpeg(hosts, path="/seagull.jpg")
    _routes(hosts, _ul(_card("/seagull", "The Seagull", f"http://{CDN_HOST}/seagull.jpg")))  # /seagull itself is a 404
    status, _ = _look(db, it)
    assert status == "error" and it.image_key is None and "not found" in it.image_note


def test_the_listing_is_fetched_once_for_all_its_events(db, hosts, stored):
    src = _on_venue(db)
    its = [_item(db, src, title=t, url=f"http://{PAGE_HOST}/{t.lower()}") for t in ("Hamlet", "Macbeth", "Othello")]
    _jpeg(hosts, path="/a.jpg")
    hosts[(CDN_HOST, "/b.jpg")] = (200, "image/jpeg", _image(colour=(10, 120, 60)))
    hosts[(CDN_HOST, "/c.jpg")] = (200, "image/jpeg", _image(colour=(30, 30, 160)))
    _routes(hosts, _ul(_card("/hamlet", "Hamlet", f"http://{CDN_HOST}/a.jpg"), _card("/macbeth", "Macbeth", f"http://{CDN_HOST}/b.jpg"),
                       _card("/othello", "Othello", f"http://{CDN_HOST}/c.jpg")), hamlet=_page(), macbeth=_page(), othello=_page())
    rows = pictures.add_item_pictures(db, NOW)
    assert [r[1] for r in rows] == ["ok", "ok", "ok"]
    assert _Hosts.asked.count((PAGE_HOST, "/season")) == 1
    assert len({i.image_key for i in db.query(DiscoveredItem)}) == 3


def test_a_picture_an_admin_set_is_not_replaced(db, hosts, stored):
    it = _item(db, _on_venue(db), image_override=True)
    _jpeg(hosts, path="/seagull.jpg")
    _routes(hosts, _ul(_card("/seagull", "The Seagull", f"http://{CDN_HOST}/seagull.jpg")), seagull=_page())
    pictures.add_item_pictures(db, NOW)
    db.refresh(it)
    assert it.image_key is None and ("venue.example", "/season") not in _Hosts.asked


def test_a_card_picture_is_not_taken_when_robots_disallows_the_listing(db, hosts, stored):
    it = _item(db, _on_venue(db))
    _jpeg(hosts, path="/seagull.jpg")
    _routes(hosts, _ul(_card("/seagull", "The Seagull", f"http://{CDN_HOST}/seagull.jpg")), seagull=_page())
    hosts[(PAGE_HOST, "/robots.txt")] = (200, "text/plain", b"User-agent: *\nDisallow: /season\n")
    assert _look(db, it)[0] == "none" and it.image_key is None


def test_items_looked_at_by_an_older_finder_get_one_more_look():
    assert pictures.FINDER_VERSION >= 2
