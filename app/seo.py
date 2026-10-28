"""Pages that search engines and link previews can read (CC-108).

The site itself is one page that draws everything with JavaScript, so Google and chat apps see almost
nothing. These are plain HTML instead: a page per live event at /e/<id>-<title>, a sitemap and
robots.txt. CloudFront sends those paths on chiculturecrew.com here and caches the answers for a few
minutes (infra/aws/frontend.tf), so a change shows up without a rebuild.

Only what the catalog already shows publicly goes on a page: never plans, crews, names or journals, except the
journal entries members chose to share there (public takes, CC-112, from app.takes).
"""
import json
import re
import unicodedata
from datetime import date
from html import escape
from xml.sax.saxutils import escape as xml_escape

from .categories import label
from .config import get_settings
from .models import Area, Event

PREVIEW_IMAGE = "/img/preview.png"  # 1200x630, in the site repo
DESCRIPTION_MAX = 160


def slug(title: str) -> str:
    """"4000 Miles" -> "4000-miles". Only for readability: the id finds the event, so a rename keeps old links working."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", title) if not unicodedata.combining(c))  # é -> e
    words = re.sub(r"[^a-z0-9]+", "-", re.sub(r"['’]", "", plain.lower())).strip("-")  # don't -> dont
    if len(words) > 60:
        words = words[:60].rsplit("-", 1)[0]
    return words or "event"


def path_for(event: Event) -> str:
    return f"/e/{event.id}-{slug(event.title)}"


def id_from(ref: str) -> int | None:
    m = re.fullmatch(r"(\d{1,9})(-[a-z0-9-]*)?", ref)
    return int(m.group(1)) if m else None


def is_closed(event: Event, today: date) -> bool:
    return event.end_date is not None and event.end_date < today


def _day(d: date, weekday: bool = False) -> str:
    return (d.strftime("%a, ") if weekday else "") + f"{d.strftime('%b')} {d.day}, {d.year}"


def dates_text(event: Event) -> str:
    b, e = event.begin_date, event.end_date
    if b and e:
        if b == e:
            return _day(b, weekday=True)
        start = f"{b.strftime('%b')} {b.day}" + (f", {b.year}" if b.year != e.year else "")
        return f"{start} – {_day(e)}"
    if b:
        return f"From {_day(b)}"
    if e:
        return f"Through {_day(e)}"
    return "Dates not posted yet"


def price_text(event: Event) -> str:
    if event.min_price is not None and event.min_price == 0:
        return "Free"
    if event.cost_text.strip():
        return event.cost_text.strip()
    if event.min_price is not None:
        return f"From ${event.min_price:,.2f}".replace(".00", "")
    return "Price not posted"


def place_text(event: Event) -> str:
    """"The Den Theatre · Wicker Park, Chicago"; suburbs already read "Oak Park, IL"."""
    v = event.venue
    where = v.neighborhood.strip()
    if v.area == Area.city:
        where = f"{where}, Chicago" if where and "chicago" not in where.lower() else (where or "Chicago")
    return f"{v.name} · {where}" if where else v.name


def _locality(event: Event) -> tuple[str, str]:
    """(town, state): "Oak Park, IL" -> ("Oak Park", "IL"), "Racine, WI" -> ("Racine", "WI"); the city is Chicago."""
    v = event.venue
    where = v.neighborhood.strip()
    if v.area == Area.city or not where:
        return "Chicago", "IL"
    m = re.fullmatch(r"(.+?),\s*([A-Z]{2})", where)
    return (m.group(1), m.group(2)) if m else (where, "IL")


def description(event: Event, today: date) -> str:
    lead = f"{label(event.category, event.opera)} at {place_text(event).replace(' · ', ' in ')}."
    if is_closed(event, today):
        text = f"{lead} This run has closed. See what else is on in Chicago on Culture Crew."
    else:
        text = f"{lead} {dates_text(event)}. {price_text(event)}. Plan it with your crew on Culture Crew."
    return text if len(text) <= DESCRIPTION_MAX else text[:DESCRIPTION_MAX - 1].rsplit(" ", 1)[0] + "…"


def json_ld(event: Event, today: date) -> dict | None:
    """schema.org Event for Google's event listings. Google needs a start date, so none without one."""
    start = event.begin_date or event.end_date
    if start is None:
        return None
    site = get_settings().site_url
    town, state = _locality(event)
    data = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": event.title,
        "startDate": start.isoformat(),
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "eventStatus": "https://schema.org/EventScheduled",
        "location": {
            "@type": "Place",
            "name": event.venue.name,
            "address": {"@type": "PostalAddress", "addressLocality": town,
                        "addressRegion": state, "addressCountry": "US"},
        },
        "description": description(event, today),
        "image": [site + PREVIEW_IMAGE],
        "url": site + path_for(event),
    }
    if event.end_date:
        data["endDate"] = event.end_date.isoformat()
    if event.min_price is not None or event.url:
        offer = {"@type": "Offer", "priceCurrency": "USD", "availability": "https://schema.org/InStock"}
        if event.min_price is not None:
            offer["price"] = f"{event.min_price:.2f}"
        if event.url:
            offer["url"] = event.url
        data["offers"] = offer
    return data


def _view_link(event: Event) -> str:
    """CC-117: the event's view in the site, with your plans and your crews' (#e/<id>, which the site opens)."""
    return f"{get_settings().site_url}/#e/{event.id}"


PAGE_STYLE = """
:root, :root[data-theme="dark"] {
  --page: #0B1220; --surface: #141D2F; --line: #26334C; --text: #EAF0F8; --muted: #A3B3CA;
  --lead: #5CC8F5; --on-lead: #04212E; --star: #FF5C70; --danger-soft: #3D1420; --on-danger-soft: #FFB3BD; --focus: #5CC8F5;
  color-scheme: dark;
}
:root[data-theme="light"] {
  --page: #F6F6F3; --surface: #FFFFFF; --line: #E2E2DC; --text: #111111; --muted: #5C5C57;
  --lead: #D62839; --on-lead: #FFFFFF; --star: #D62839; --danger-soft: #FBE4E6; --on-danger-soft: #9E1B28; --focus: #1D4ED8;
  color-scheme: light;
}
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text); font: 1rem/1.55 "Figtree", system-ui, -apple-system, "Segoe UI", sans-serif; -webkit-font-smoothing: antialiased; }
.wrap { max-width: 760px; margin: 0 auto; padding: 24px max(20px, env(safe-area-inset-right)) 56px max(20px, env(safe-area-inset-left)); }
a { color: var(--lead); }
a:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; border-radius: 4px; }
.brand { display: inline-flex; align-items: center; gap: 8px; font: 600 1.25rem "Outfit", "Figtree", system-ui, sans-serif; color: var(--text); text-decoration: none; }
.brand svg { width: 20px; height: 20px; fill: var(--star); }
header { padding-bottom: 18px; border-bottom: 1px solid var(--line); margin-bottom: 36px; }
.eyebrow { margin: 0 0 8px; font-size: 0.8125rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
h1 { margin: 0 0 12px; font: 600 clamp(2rem, 6vw, 3rem)/1.08 "Outfit", "Figtree", system-ui, sans-serif; text-wrap: balance; }
.place { margin: 0 0 24px; color: var(--muted); font-size: 1.0625rem; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 10px 20px; margin: 0 0 28px; padding: 18px 20px; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; }
dt { color: var(--muted); font-size: 0.875rem; font-weight: 600; }
dd { margin: 0; font-variant-numeric: tabular-nums; }
.closed { display: inline-block; margin: 0 0 20px; padding: 4px 10px; border-radius: 999px; background: var(--danger-soft); color: var(--on-danger-soft); font-weight: 600; font-size: 0.875rem; }
.actions { display: flex; flex-wrap: wrap; gap: 12px; }
.btn { display: inline-flex; align-items: center; min-height: 44px; padding: 10px 18px; border-radius: 8px; font-weight: 600; text-decoration: none; border: 1px solid var(--lead); }
.btn.primary { background: var(--lead); color: var(--on-lead); }
.btn.secondary { color: var(--lead); }
.note { margin-top: 36px; color: var(--muted); font-size: 0.875rem; }
.takes { margin-top: 44px; }
.takes h2 { margin: 0 0 4px; font: 600 1.375rem/1.2 "Outfit", "Figtree", system-ui, sans-serif; }
.takes ol { list-style: none; margin: 0; padding: 0; }
.take { padding: 20px 0; border-bottom: 1px solid var(--line); }
.take-words { margin: 0 0 6px; font: 600 1.5rem/1.2 "Outfit", "Figtree", system-ui, sans-serif; letter-spacing: -0.01em; overflow-wrap: anywhere; }
.take-words span + span::before { content: " · "; color: var(--star); }
.take-review { margin: 0 0 8px; white-space: pre-line; overflow-wrap: anywhere; }
.take-by { margin: 0; color: var(--muted); font-size: 0.875rem; }
.take-by b { color: var(--text); font-weight: 600; }
"""

THEME_SCRIPT = """(function () {
  var t = "dark";
  try { var s = localStorage.getItem("cc_theme"); if (s === "light" || s === "dark") t = s; } catch (e) {}
  document.documentElement.setAttribute("data-theme", t);
})();"""

STAR = ('<svg aria-hidden="true" viewBox="0 0 24 24"><polygon points="12,1 14.5,7.67 21.53,6.5 17,12 21.53,17.5 '
        '14.5,16.33 12,23 9.5,16.33 2.47,17.5 7,12 2.47,6.5 9.5,7.67"></polygon></svg>')


def _page(title: str, desc: str, canonical: str | None, body: str, extra_head: str = "", noindex: bool = False) -> str:
    site = get_settings().site_url
    e = lambda s: escape(s, quote=True)  # noqa: E731
    head = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        f"<script>{THEME_SCRIPT}</script>",
        f"<title>{e(title)}</title>",
        f'<meta name="description" content="{e(desc)}">',
    ]
    if noindex:
        head.append('<meta name="robots" content="noindex">')
    if canonical:
        head += [
            f'<link rel="canonical" href="{e(canonical)}">',
            '<meta property="og:type" content="website">',
            '<meta property="og:site_name" content="Culture Crew">',
            f'<meta property="og:title" content="{e(title)}">',
            f'<meta property="og:description" content="{e(desc)}">',
            f'<meta property="og:url" content="{e(canonical)}">',
            f'<meta property="og:image" content="{e(site + PREVIEW_IMAGE)}">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            '<meta name="twitter:card" content="summary_large_image">',
        ]
    head += [
        '<link rel="preconnect" href="https://fonts.googleapis.com">',
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Figtree:wght@400..700&family=Outfit:wght@600&display=swap">',
        f"<style>{PAGE_STYLE}</style>",
        extra_head,
    ]
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n" + "\n".join(h for h in head if h) + "\n</head>\n<body>\n"
            f'<div class="wrap">\n<header><a class="brand" href="{e(site)}/">{STAR}Culture Crew</a></header>\n'
            f"<main>\n{body}\n</main>\n</div>\n</body>\n</html>\n")


def _takes_html(takes: list[dict]) -> str:
    """CC-112: members' shared journal entries: the three words, the review, the name and whether they'd recommend it."""
    if not takes:
        return ""
    e = lambda s: escape(s, quote=True)  # noqa: E731
    items = []
    for t in takes:
        words = "".join(f"<span>{e(w)}</span>" for w in t["words"].split())
        review = f'<p class="take-review">{e(t["review"])}</p>' if t["review"] else ""
        by = f"<b>{e(t['name'])}</b>" + (" · Would recommend" if t["recommend"] else "")
        items.append(f'<li class="take"><p class="take-words">{words}</p>{review}<p class="take-by">{by}</p></li>')
    return ('<section class="takes" aria-labelledby="takes-h"><h2 id="takes-h">From people who went</h2>'
            f'<ol>{"".join(items)}</ol></section>')


def render_event(event: Event, today: date, takes: list[dict] = ()) -> str:
    site = get_settings().site_url
    e = lambda s: escape(s, quote=True)  # noqa: E731
    closed = is_closed(event, today)
    title = f"{event.title} · {event.venue.name} · Culture Crew"
    desc = description(event, today)
    rows = [("When", dates_text(event)), ("Price", price_text(event))]  # where is right under the title
    body = [f'<p class="eyebrow">{e(label(event.category, event.opera))}</p>', f"<h1>{e(event.title)}</h1>",
            f'<p class="place">{e(place_text(event))}</p>']
    if closed:
        body.append('<p class="closed">This run has closed</p>')
    body.append("<dl>" + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in rows) + "</dl>")
    actions = []
    if event.url and not closed:
        actions.append(f'<a class="btn primary" href="{e(event.url)}" rel="noopener">Tickets and showtimes ↗</a>')
    actions.append(f'<a class="btn {"secondary" if actions else "primary"}" href="{e(_view_link(event))}">Open in Culture Crew</a>')
    body.append(f'<div class="actions">{"".join(actions)}</div>')
    if takes:
        body.append(_takes_html(list(takes)))
    body.append('<p class="note">Dates and prices change, so check the event’s own page before you go.</p>')
    ld = json_ld(event, today)
    extra = ""
    if ld and not closed:
        # "</" can't appear inside a script block, so the JSON escapes it
        extra = '<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>"
    return _page(title, desc, site + path_for(event), "\n".join(body), extra)


def render_missing() -> str:
    site = escape(get_settings().site_url, quote=True)
    body = ('<h1>That event isn’t on the list</h1>\n<p class="place">It may have been taken down, or the link is wrong.</p>\n'
            f'<div class="actions"><a class="btn primary" href="{site}/#catalog">Browse the catalog</a></div>')
    return _page("Event not found · Culture Crew", "That event isn't on Culture Crew's list.", None, body, noindex=True)


def sitemap(events: list[Event], today: date) -> str:
    site = get_settings().site_url
    urls = [(site + "/", None)] + [(site + path_for(ev), ev.updated_at.date() if ev.updated_at else None)
                                   for ev in events if not is_closed(ev, today)]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, mod in urls:
        lines.append(f"  <url><loc>{xml_escape(loc)}</loc>" + (f"<lastmod>{mod.isoformat()}</lastmod>" if mod else "") + "</url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def robots() -> str:
    return f"User-agent: *\nAllow: /\n\nSitemap: {get_settings().site_url}/sitemap.xml\n"
