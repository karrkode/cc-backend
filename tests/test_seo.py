"""CC-108: plain HTML event pages, the sitemap and robots.txt, for search engines and link previews."""
import json
import re
from datetime import timedelta
from decimal import Decimal

from app.models import Area, EventStatus
from app.seo import slug
from app.stats import today_in_chicago

TODAY = today_in_chicago()


def ld_of(html: str) -> dict:
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert m, "no JSON-LD on the page"
    return json.loads(m.group(1))


def test_event_page_is_readable_without_javascript(client, make_event):
    ev = make_event(title="4000 Miles", venue_name="The Den Theatre", neighborhood="Wicker Park",
                    begin=TODAY - timedelta(days=3), end=TODAY + timedelta(days=10), min_price=Decimal("20"), cost_text="$20–$30")
    r = client.get(f"/e/{ev.id}-4000-miles")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=600"
    page = r.text
    assert "<title>4000 Miles · The Den Theatre · Culture Crew</title>" in page
    assert "<h1>4000 Miles</h1>" in page and "The Den Theatre · Wicker Park, Chicago" in page and "$20–$30" in page
    assert f"{TODAY.year}" in page and 'rel="canonical"' in page and 'property="og:image"' in page
    assert f'href="https://example.org/4000 miles"' in page  # the event's own page, for tickets
    assert f'href="http://localhost:4173/#e/{ev.id}"' in page or f'/#e/{ev.id}"' in page  # opens the event view (CC-117)


def test_json_ld_describes_the_event(client, make_event):
    ev = make_event(title="Wait Wait", venue_name="Studebaker", neighborhood="Loop",
                    begin=TODAY, end=TODAY + timedelta(days=7), min_price=Decimal("0"))
    ld = ld_of(client.get(f"/e/{ev.id}-wait-wait").text)
    assert ld["@type"] == "Event" and ld["name"] == "Wait Wait"
    assert ld["startDate"] == TODAY.isoformat() and ld["endDate"] == (TODAY + timedelta(days=7)).isoformat()
    assert ld["location"]["name"] == "Studebaker"
    assert ld["location"]["address"] == {"@type": "PostalAddress", "addressLocality": "Chicago", "addressRegion": "IL", "addressCountry": "US"}
    assert ld["offers"]["price"] == "0.00" and ld["offers"]["priceCurrency"] == "USD"


def test_out_of_town_venues_get_their_own_state(client, make_event):
    ev = make_event(title="Jewelry", venue_name="Racine Art Museum", neighborhood="Racine, WI", area=Area.out_of_town)
    addr = ld_of(client.get(f"/e/{ev.id}-jewelry").text)["location"]["address"]
    assert (addr["addressLocality"], addr["addressRegion"]) == ("Racine", "WI")


def test_old_titles_and_bare_ids_redirect_to_the_current_address(client, make_event):
    ev = make_event(title="Dead Girl's Quinceañera")
    for ref in (str(ev.id), f"{ev.id}-an-old-title"):
        r = client.get(f"/e/{ref}", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == f"/e/{ev.id}-dead-girls-quinceanera"


def test_unpublished_and_unknown_events_are_not_found(client, make_event):
    pending = make_event(title="Secret", status=EventStatus.pending)
    for ref in (f"{pending.id}-secret", "999999-nope", "not-an-id"):
        r = client.get(f"/e/{ref}")
        assert r.status_code == 404 and 'name="robots" content="noindex"' in r.text and "Secret" not in r.text


def test_closed_event_says_so_and_has_no_event_data(client, make_event):
    ev = make_event(title="Gone", begin=TODAY - timedelta(days=30), end=TODAY - timedelta(days=1))
    page = client.get(f"/e/{ev.id}-gone").text
    assert "This run has closed" in page and "application/ld+json" not in page and "Tickets and showtimes" not in page


def test_sitemap_lists_live_running_events_only(client, make_event):
    live = make_event(title="Running")
    closed = make_event(title="Closed", begin=TODAY - timedelta(days=9), end=TODAY - timedelta(days=2))
    pending = make_event(title="Pending", status=EventStatus.pending)
    r = client.get("/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert r.text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert f"/e/{live.id}-running</loc>" in r.text
    assert f"/e/{closed.id}-" not in r.text and f"/e/{pending.id}-" not in r.text
    assert r.text.count("/e/") == 1 and "<loc>http://localhost:4173/</loc>" in r.text  # home and the running event (and CC-109's roundups)


def test_robots_points_to_the_sitemap(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Allow: /" in r.text and r.text.strip().endswith("/sitemap.xml")


def test_nothing_private_on_an_event_page(client, make_event, user_token):
    from .conftest import auth
    ev = make_event(title="Private Check")
    client.put(f"/me/plans/{ev.id}", json={"status": "interested"}, headers=auth(user_token))
    page = client.get(f"/e/{ev.id}-private-check").text
    assert "Ana" not in page and "@example.com" not in page and "interested" not in page.lower()


def test_slug():
    assert slug("Tanya Crane: Embodied Histories—Exploring") == "tanya-crane-embodied-histories-exploring"
    assert slug("Wait Wait... Don't Tell Me!") == "wait-wait-dont-tell-me"
    assert slug("!!!") == "event" and len(slug("word " * 40)) <= 60


OLD_LOOK = ("Figtree", "Outfit", "border-radius", "999px", "text-transform", "eyebrow", "pill")


def test_event_page_is_a_small_lakefront_poster(client, make_event, db):
    """CC-151: Lakefront type and shapes, the compact masthead, then the poster on its category field, the facts table
    and the main button into the site. The link-preview tags and structured data stay as they were."""
    ev = make_event(title="Tosca", category="Music", venue_name="Lyric Opera", begin=TODAY, end=TODAY + timedelta(days=7))
    ev.opera = True
    db.commit()
    page = client.get(f"/e/{ev.id}-tosca").text
    for old in OLD_LOOK:
        assert old not in page, old
    assert "Newsreader" in page and "Big+Shoulders+Text" in page and 'var t = "light"' in page
    assert '<header class="mast"><div class="wrap"><a class="brand"' in page and "border-bottom: 6px solid var(--stripe)" in page
    assert '<div class="poster cat-music"><div class="poster-in"><p class="poster-k">Opera</p><h1>Tosca</h1>' in page
    assert '<dl class="facts"><dt>When</dt>' in page
    actions = page.split('<div class="actions">')[1]
    assert actions.startswith(f'<a class="btn primary" href="http://localhost:4173/#e/{ev.id}">Open in Culture Crew</a>')
    assert '<a class="btn secondary" href="https://example.org/tosca"' in actions
    assert "min-height: 48px" in page and ":focus-visible { outline: 3px solid var(--focus)" in page
    for tag in ('property="og:title" content="Tosca · Lyric Opera · Culture Crew"', 'property="og:image"',
                'name="twitter:card" content="summary_large_image"', 'rel="canonical"'):
        assert tag in page
    assert ld_of(page)["name"] == "Tosca"


def test_missing_event_page_is_lakefront_too(client):
    page = client.get("/e/999999-nope").text
    for old in OLD_LOOK:
        assert old not in page, old
    assert '<div class="lost"><h1>That event isn’t on the list</h1>' in page and 'class="deck"' in page
