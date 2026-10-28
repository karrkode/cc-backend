"""Reading events from a page's own structured data, before asking Claude (CC-80).

Many sites describe their events for search engines as schema.org data in <script type="application/ld+json">
(Goodman's show pages do, for example). When a page has it, discovery reads the events from it directly:
no tokens, and no chance of misreading. Claude is used only when a page has none (or, for a list page,
too few to trust: see run.MIN_LISTED).

Kinds and types come from the source (a theater's events are productions), since this data doesn't say
which of the owner's categories an event belongs in. The leave-out rules still apply (classes by title).
"""
import html
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from urllib.parse import urljoin

from .readers import FoundEvent, Performance
from .rules import norm_title

SCRIPTS = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
CANCELLED = ("EventCancelled", "EventPostponed")
# by source kind: (kind, category); CC-125: opera is Music (and marked opera when stored), museums Visual Art
BY_SOURCE = {"theater": ("production", "Theater"), "opera": ("production", "Music"), "music": ("concert", "Music"),
             "comedy": ("comedy_show", "Comedy"), "museum": ("exhibition", "Visual Art")}
MERGE_SAME_TITLE = {"theater", "opera", "museum"}  # one event per production or exhibition; concerts and comedy are one per night


def _objects(data):
    """Every JSON object in a ld+json block, @graph and nested lists included."""
    if isinstance(data, list):
        for x in data:
            yield from _objects(x)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "subEvent", "itemListElement", "item"):
            if key in data:
                yield from _objects(data[key])


def _is_event(obj: dict) -> bool:
    types = obj.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.endswith("Event") for t in types)


def _when(value) -> tuple[str | None, str | None]:
    """"2026-11-05T19:30:00-06:00" -> ("2026-11-05", "19:30"); "2026-11-05" -> ("2026-11-05", None). The
    time is the venue's local time as written. CC-134: "20261231" (Goodman's endDate) is read too."""
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})(?!\d)(?:[T ](\d{2}):?(\d{2}))?", str(value or "").strip())
    if not m:
        return None, None
    try:
        day = date(*map(int, m.group(1, 2, 3))).isoformat()
    except ValueError:
        return None, None
    t = f"{m.group(4)}:{m.group(5)}" if m.group(4) else None
    return day, (t if t and t != "00:00" else None)


def _price(offers) -> tuple[str, float | None]:
    prices = []
    for o in offers if isinstance(offers, list) else [offers]:
        if not isinstance(o, dict):
            continue
        for key in ("price", "lowPrice", "highPrice"):
            try:
                if o.get(key) not in (None, ""):
                    prices.append(Decimal(str(o[key]).replace("$", "").replace(",", "")))
            except InvalidOperation:
                pass
    if not prices:
        return "", None
    lo, hi = min(prices), max(prices)
    if hi == 0:
        return "Free", 0.0
    text = f"${_money(lo)}" if lo == hi else f"${_money(lo)}–${_money(hi)}"
    return text, float(lo)


def _money(d: Decimal) -> str:
    return f"{d:.0f}" if d == d.to_integral_value() else f"{d:.2f}"


def _text(v) -> str:
    return html.unescape(str(v or "")).strip()


def events_in(page_html: str, page_url: str, source_kind: str) -> list[FoundEvent]:
    kind, category = BY_SOURCE.get(source_kind, ("other", None))
    raw = []
    for block in SCRIPTS.findall(page_html or ""):
        try:
            data = json.loads(html.unescape(block.strip()) if "&quot;" in block else block.strip())
        except ValueError:
            continue
        raw += [o for o in _objects(data) if _is_event(o) and _text(o.get("name"))]
    events: dict[str, FoundEvent] = {}
    for o in raw:
        if any(str(o.get("eventStatus", "")).endswith(c) for c in CANCELLED):
            continue
        title = _text(o.get("name"))[:300]
        start, start_time = _when(o.get("startDate"))
        end, _ = _when(o.get("endDate"))
        cost, low = _price(o.get("offers"))
        url = urljoin(page_url, _text(o.get("url"))) if o.get("url") else ""
        shows = [Performance(date=start, time=start_time)] if start and start_time else []
        key = norm_title(title) if source_kind in MERGE_SAME_TITLE else f"{norm_title(title)}|{start}"
        if key in events:  # another performance of the same production
            e = events[key]
            e.performances += [p for p in shows if p not in e.performances]
            e.begin_date = min(filter(None, [e.begin_date, start]), default=None)
            e.end_date = max(filter(None, [e.end_date, end or start]), default=None)
            e.cost_text, e.min_price = e.cost_text or cost, e.min_price if e.min_price is not None else low
            continue
        single = kind if not (source_kind == "museum" and start and (end or start) == start) else "program"
        # the location's name is often a stage ("Albert Theatre"), not a venue, so it's left to the source's venue
        events[key] = FoundEvent(title=title, url=url or None, kind=single, category=category,
                                 begin_date=start, end_date=end or (start if source_kind not in MERGE_SAME_TITLE else None),
                                 cost_text=cost or None, min_price=low, performances=shows)
    return list(events.values())


def complete(e: FoundEvent) -> bool:
    """Enough to stand in for a reading by Claude: dates (a run, or dated performances) and a price.
    Thinner data (Goodman's gives a start date only) goes to Claude instead, so nothing is lost."""
    dated = bool(e.performances) or bool(e.begin_date and e.end_date)
    return dated and (e.cost_text is not None or e.min_price is not None)


# CC-124: what a member's link says beyond the event itself, which discovery takes from the source instead
BY_EVENT_TYPE = {"TheaterEvent": "Theater", "ComedyEvent": "Comedy", "MusicEvent": "Music", "DanceEvent": "Theater",
                 "ExhibitionEvent": "Visual Art", "VisualArtsEvent": "Visual Art", "EducationEvent": "Educational"}


def _place(location) -> tuple[str, str]:
    """(name, town) of an event's location: the first Place, with its address's locality."""
    for loc in location if isinstance(location, list) else [location]:
        if isinstance(loc, dict) and _text(loc.get("name")):
            addr = loc.get("address")
            town = _text(addr.get("addressLocality")) if isinstance(addr, dict) else ""
            return _text(loc.get("name"))[:200], town[:120]
    return "", ""


def event_places(page_html: str) -> dict[str, dict]:
    """By normalized event name: its venue name, town, category (from its schema.org type) and whether the page gives
    a closing date (events_in takes the opening date for a missing one, which a member's link shouldn't)."""
    found = {}
    for block in SCRIPTS.findall(page_html or ""):
        try:
            data = json.loads(html.unescape(block.strip()) if "&quot;" in block else block.strip())
        except ValueError:
            continue
        for o in _objects(data):
            if not (_is_event(o) and _text(o.get("name"))):
                continue
            types = o.get("@type") if isinstance(o.get("@type"), list) else [o.get("@type")]
            category = next((BY_EVENT_TYPE[t] for t in types if t in BY_EVENT_TYPE), None)
            venue, town = _place(o.get("location"))
            found.setdefault(norm_title(_text(o.get("name"))[:300]), {"venue": venue, "town": town, "category": category,
                                                                       "end_given": bool(_when(o.get("endDate"))[0])})
    return found


# CC-134: the page's own title, which a visitor sees, against its event data, which can be out of date (Goodman's
# "A Christmas Carol 2026" page calls its event "A Christmas Carol 2027")
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
OG_TITLE = re.compile(r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]*>', re.I)
CONTENT = re.compile(r'content=["\']([^"\']*)["\']', re.I)
SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]*$")  # " - Goodman Theatre"
SAME_SHOW = 0.8  # how alike the two must be: a different year or a typo, not a list page's own title


def page_titles(page_html: str) -> list[str]:
    """The page's <title>, og:title, WebPage name and last breadcrumb, as written."""
    found = [m.group(1) for m in TITLE.finditer(page_html or "")][:1]
    found += [c.group(1) for m in OG_TITLE.finditer(page_html or "") if (c := CONTENT.search(m.group(0)))]
    for block in SCRIPTS.findall(page_html or ""):
        try:
            data = json.loads(html.unescape(block.strip()) if "&quot;" in block else block.strip())
        except ValueError:
            continue
        for o in _objects(data):
            if o.get("@type") == "WebPage":
                found.append(o.get("name"))
            elif o.get("@type") == "BreadcrumbList" and isinstance(o.get("itemListElement"), list) and o["itemListElement"]:
                last = o["itemListElement"][-1]
                if isinstance(last, dict):
                    item = last.get("item")
                    found.append(last.get("name") or (item.get("name") if isinstance(item, dict) else None))
    return [t for t in (_text(f) for f in found if isinstance(f, str)) if t]


def visible_title(name: str, page_html: str) -> str:
    """name, unless the page's own title is nearly the same but not quite: then that title, without a venue suffix."""
    want = norm_title(name)
    best, score = name, 0.0
    for title in page_titles(page_html):
        for t in (title, SUFFIX.sub("", title)):
            got = norm_title(t)
            if got == want:
                return name  # the page agrees with its event data
            if got and (r := SequenceMatcher(None, want, got).ratio()) > score:
                best, score = t.strip(), r
    return best[:300] if score >= SAME_SHOW else name
