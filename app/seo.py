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


# CC-151: Lakefront, as in the site (site.css, CC-136): the same tokens and type, the compact masthead (CC-137) and square
# corners. The 404 page (cc-site public/404.html, CC-149) is the model for a standalone page.
PAGE_STYLE = """
:root, :root[data-theme="light"] {
  --page: #FFFFFF; --surface: #F2F7F9; --line: #D3E3EA; --rule: #0E2A40; --text: #0E2A40; --muted: #4A6275;
  --lake: #0E3A5B; --on-lake: #FFFFFF; --stripe: #9CD3EF; --lead: #0E3A5B; --on-lead: #FFFFFF; --star: #C8102E; --focus: #0F5E8C;
  --cat-theater: #B9472E; --cat-music: #5B3E9E; --cat-art: #2F7A5B;
  color-scheme: light;
}
:root[data-theme="dark"] {
  --page: #0A1A28; --surface: #0F2334; --line: #22394D; --rule: #A6BDCC; --text: #EAF2F7; --muted: #A6BDCC;
  --lake: #06121C; --on-lake: #EAF2F7; --stripe: #5E9FC2; --lead: #9CD3EF; --on-lead: #06121C; --star: #FF5C70; --focus: #9CD3EF;
  --cat-theater: #B9472E; --cat-music: #7A5AD0; --cat-art: #2F7A5B;
  color-scheme: dark;
}
:root { --on-cat: #FFFFFF; --display: "Newsreader", Georgia, "Times New Roman", serif;
  --sign: "Big Shoulders Text", "Arial Narrow", system-ui, sans-serif; }
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text); font-family: var(--display); font-size: 1.125rem; line-height: 1.45;
  -webkit-font-smoothing: antialiased; }
.wrap { max-width: 1120px; margin-inline: auto;
  padding-inline: max(16px, min(4vw, 40px), env(safe-area-inset-left)) max(16px, min(4vw, 40px), env(safe-area-inset-right)); }
a { color: var(--lead); text-underline-offset: 3px; }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.mast { background: var(--lake); color: var(--on-lake); border-bottom: 6px solid var(--stripe); }
.mast .wrap { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 4px 20px; min-height: 72px; }
.brand { display: inline-flex; align-items: center; gap: 6px; min-height: 44px; color: inherit; text-decoration: none;
  font-weight: 600; font-size: 2rem; letter-spacing: -0.015em; }
.star { width: 0.7em; height: 0.7em; fill: var(--star); }
.navlink { display: inline-flex; align-items: center; min-height: 44px; color: inherit; font-family: var(--sign); font-weight: 700;
  font-size: 1.1875rem; text-decoration: none; }
.navlink:hover { text-decoration: underline 3px var(--stripe); text-underline-offset: 8px; }
.mast a:focus-visible { outline-color: var(--stripe); }
main { padding-bottom: 64px; }
h1, h2, p { margin: 0; }
.cat-theater { --cat: var(--cat-theater); } .cat-music { --cat: var(--cat-music); } .cat-art { --cat: var(--cat-art); }
.cat-none { --cat: var(--lake); }
.back { display: inline-flex; align-items: center; min-height: 44px; margin-top: 12px; color: var(--text); font-size: 1.1875rem; text-decoration: none; }
.back:hover { text-decoration: underline; }
.poster { display: flex; align-items: flex-end; min-height: 300px; margin-top: 4px; padding: clamp(20px, 4vw, 40px);
  background: var(--cat); color: var(--on-cat); }
.poster-in { display: grid; gap: 10px; max-width: 920px; min-width: 0; }
.poster-k { font-family: var(--sign); font-weight: 700; font-size: 1.25rem; line-height: 1.2; }
.poster h1 { font-weight: 500; font-size: clamp(2.5rem, 6.5vw, 4.5rem); line-height: 0.98; letter-spacing: -0.02em; overflow-wrap: anywhere; text-wrap: balance; }
.poster-where { font-size: 1.5rem; line-height: 1.25; }
.ev-main { max-width: 720px; margin-top: 32px; }
.facts { display: grid; grid-template-columns: max-content minmax(0, 1fr); margin: 0; border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); }
.facts dt, .facts dd { margin: 0; padding: 12px 0; border-top: 1px solid var(--line); }
.facts dt:first-of-type, .facts dd:first-of-type { border-top: 0; }
.facts dt { padding-right: 32px; font-family: var(--sign); font-weight: 700; font-size: 1.125rem; line-height: 1.6; color: var(--muted); }
.facts dd { font-size: 1.375rem; line-height: 1.3; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.closed { margin-top: 12px; font-family: var(--sign); font-weight: 700; font-size: 1.125rem; color: var(--muted); }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 24px; }
.btn { display: inline-flex; align-items: center; min-height: 48px; padding: 0 20px; font-family: var(--sign); font-weight: 700;
  font-size: 1.1875rem; line-height: 1.1; text-decoration: none; }
.btn.primary { background: var(--lead); color: var(--on-lead); }
.btn.primary:hover { filter: brightness(1.08); }
.btn.secondary { color: var(--text); box-shadow: inset 0 0 0 1.5px var(--text); }
.btn.secondary:hover { background: var(--surface); }
.takes { margin-top: 48px; }
.takes h2 { padding-bottom: 6px; border-bottom: 1px solid var(--rule); font-family: var(--sign); font-weight: 700; font-size: 1.375rem; }
.takes ol { list-style: none; margin: 0; padding: 0; }
.take { display: grid; gap: 6px; padding: 16px 0; border-bottom: 1px solid var(--line); }
.take-words { font-style: italic; font-size: 1.625rem; line-height: 1.2; overflow-wrap: anywhere; }
.take-words span + span::before { content: " · "; color: var(--muted); font-style: normal; }
.take-review { white-space: pre-line; overflow-wrap: anywhere; }
.take-by { font-family: var(--sign); font-weight: 700; font-size: 1rem; color: var(--muted); }
.take-by b { color: var(--text); }
.note { margin-top: 48px; color: var(--muted); font-size: 1rem; }
.lost { padding-top: 48px; }
.lost h1 { max-width: 18ch; font-weight: 500; font-size: clamp(2.5rem, 6vw, 4rem); line-height: 1; letter-spacing: -0.025em; text-wrap: balance; }
.deck { max-width: 46ch; margin-top: 14px; font-style: italic; font-size: 1.3125rem; line-height: 1.4; color: var(--muted); }
@media (max-width: 760px) {
  .brand { font-size: 1.625rem; } .mast .wrap { min-height: 60px; } .lost { padding-top: 32px; }
  .poster { min-height: 240px; } .poster-where { font-size: 1.3125rem; }
}
"""

# Light leads (CC-136); a visitor who chose "Lights down" on the site gets dark here too.
THEME_SCRIPT = """(function () {
  var t = "light";
  try { var s = localStorage.getItem("cc_theme"); if (s === "light" || s === "dark") t = s; } catch (e) {}
  document.documentElement.setAttribute("data-theme", t);
})();"""

STAR = ('<svg class="star" aria-hidden="true" viewBox="0 0 24 24"><polygon points="12,1 14.5,7.67 21.53,6.5 17,12 21.53,17.5 '
        '14.5,16.33 12,23 9.5,16.33 2.47,17.5 7,12 2.47,6.5 9.5,7.67"></polygon></svg>')
FONTS = ("https://fonts.googleapis.com/css2?family=Big+Shoulders+Text:wght@600..800"
         "&family=Newsreader:ital,opsz,wght@0,6..72,400..600;1,6..72,400&display=swap")

_CAT_CLASS = {"Theater": "cat-theater", "Music": "cat-music", "Visual Art": "cat-art"}


def cat_class(event: Event) -> str:
    """CC-151: the class that gives a tile or poster its category field (an opera is Music's)."""
    return "cat-music" if event.opera else _CAT_CLASS.get(event.category, "cat-none")


def _page(title: str, desc: str, canonical: str | None, body: str, extra_head: str = "", noindex: bool = False,
          nav: str = "") -> str:
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
        f'<link rel="stylesheet" href="{e(FONTS)}">',
        f"<style>{PAGE_STYLE}</style>",
        extra_head,
    ]
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n" + "\n".join(h for h in head if h) + "\n</head>\n<body>\n"
            f'<header class="mast"><div class="wrap"><a class="brand" href="{e(site)}/">Culture{STAR}Crew</a>{nav}</div></header>\n'
            f'<main class="wrap">\n{body}\n</main>\n</body>\n</html>\n')


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
    rows = [("When", dates_text(event)), ("Price", price_text(event))]  # where is on the poster, under the title
    # CC-151: a small version of the site's event poster (CC-142): the category field with the kicker, title and venue
    body = [f'<a class="back" href="{e(site)}/">← The listings</a>',
            f'<div class="poster {cat_class(event)}"><div class="poster-in">'
            f'<p class="poster-k">{e(label(event.category, event.opera))}</p><h1>{e(event.title)}</h1>'
            f'<p class="poster-where">{e(place_text(event))}</p></div></div>', '<div class="ev-main">']
    body.append('<dl class="facts">' + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in rows) + "</dl>")
    if closed:
        body.append('<p class="closed">This run has closed</p>')
    # CC-151: the main button opens the event in the site; its tickets page comes second
    actions = [f'<a class="btn primary" href="{e(_view_link(event))}">Open in Culture Crew</a>']
    if event.url and not closed:
        actions.append(f'<a class="btn secondary" href="{e(event.url)}" rel="noopener">Tickets and showtimes ↗</a>')
    body.append(f'<div class="actions">{"".join(actions)}</div>')
    if takes:
        body.append(_takes_html(list(takes)))
    body.append('<p class="note">Dates and prices change, so check the event’s own page before you go.</p></div>')
    ld = json_ld(event, today)
    extra = ""
    if ld and not closed:
        # "</" can't appear inside a script block, so the JSON escapes it
        extra = '<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>"
    return _page(title, desc, site + path_for(event), "\n".join(body), extra)


def render_missing() -> str:
    site = escape(get_settings().site_url, quote=True)
    body = ('<div class="lost"><h1>That event isn’t on the list</h1>\n<p class="deck">It may have been taken down, or the link is wrong.</p>\n'
            f'<div class="actions"><a class="btn primary" href="{site}/#catalog">Browse the catalog</a></div></div>')
    return _page("Event not found · Culture Crew", "That event isn't on Culture Crew's list.", None, body, noindex=True)


def sitemap(events: list[Event], today: date, pages: list[str] = ()) -> str:
    """pages: other paths to list, such as the roundups (CC-109)."""
    site = get_settings().site_url
    urls = [(site + "/", None)] + [(site + p, None) for p in pages] + [
        (site + path_for(ev), ev.updated_at.date() if ev.updated_at else None) for ev in events if not is_closed(ev, today)]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, mod in urls:
        lines.append(f"  <url><loc>{xml_escape(loc)}</loc>" + (f"<lastmod>{mod.isoformat()}</lastmod>" if mod else "") + "</url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def robots() -> str:
    return f"User-agent: *\nAllow: /\n\nSitemap: {get_settings().site_url}/sitemap.xml\n"
