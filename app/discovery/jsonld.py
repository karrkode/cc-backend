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
from collections import deque
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from urllib.parse import urljoin

from .readers import FoundEvent, Performance
from .rules import norm_title

# CC-182: tags are found by plain searches that only move forward, so reading a page takes time in proportion to
# its size whatever its markup; and at most MAX_DATA characters of event data are parsed
SCRIPT_OPEN, SCRIPT_CLOSE = re.compile("<script", re.I), re.compile("</script", re.I)
LD_TYPE = re.compile(r'type=["\']application/ld\+json["\']', re.I)
MAX_TAG = 4_000  # an opening tag longer than this isn't one we read
MAX_DATA = 1_000_000
MAX_STREET = 400  # CC-218: longer than any street; app/addresses.py holds the real limit
# CC-218: "78 E Washington St, Chicago, IL 60602" (the state and ZIP may be missing)
WHOLE_ADDRESS = re.compile(r"([^,]+),\s*([^,\d]+?)(?:,\s*[A-Za-z]{2})?(?:\s+(\d{5}(?:-\d{4})?))?(?:,\s*(?:US|USA))?")
CANCELLED = ("EventCancelled", "EventPostponed")
# by source kind: (kind, category); CC-125: opera is Music (and marked opera when stored), museums Visual Art
BY_SOURCE = {"theater": ("production", "Theater"), "opera": ("production", "Music"), "music": ("concert", "Music"),
             "comedy": ("comedy_show", "Comedy"), "museum": ("exhibition", "Visual Art")}
MERGE_SAME_TITLE = {"theater", "opera", "museum"}  # one event per production or exhibition; concerts and comedy are one per night


def _tags(page_html: str, opening, closing=None):
    """(opening tag, its content up to the closing tag) for each tag that opening finds. Each search starts where
    the last one ended."""
    pos = 0
    while (m := opening.search(page_html, pos)):
        end = page_html.find(">", m.end())
        if end < 0:
            return
        tag, pos = page_html[m.start():end + 1], end + 1
        if len(tag) > MAX_TAG:
            continue
        if closing is None:
            yield tag, ""
            continue
        c = closing.search(page_html, pos)
        if not c:
            return
        yield tag, page_html[pos:c.start()]
        pos = c.end()


def _blocks(page_html: str):
    """The parsed JSON of each ld+json block, up to MAX_DATA characters in all. Blocks that don't parse, or don't
    fit in what's left, are skipped."""
    left = MAX_DATA
    for tag, block in _tags(page_html or "", SCRIPT_OPEN, SCRIPT_CLOSE):
        if not LD_TYPE.search(tag):
            continue
        block = block.strip()
        if len(block) > left:
            continue
        left -= len(block)
        try:
            yield json.loads(html.unescape(block) if "&quot;" in block else block)
        except (ValueError, RecursionError):
            continue


def _objects(data):
    """Every JSON object in a ld+json block, @graph and nested lists included, in page order. CC-182: walked with a
    list rather than by recursion, so deep nesting can't run out of stack."""
    todo = [data]
    while todo:
        x = todo.pop()
        if isinstance(x, list):
            todo += reversed(x)
        elif isinstance(x, dict):
            yield x
            todo += [x[key] for key in ("item", "itemListElement", "subEvent", "@graph") if key in x]


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
                if o.get(key) not in (None, "") and _sane(d := Decimal(str(o[key])[:40].replace("$", "").replace(",", ""))):
                    prices.append(d)
            except InvalidOperation:
                pass
    if not prices:
        return "", None
    lo, hi = min(prices), max(prices)
    if hi == 0:
        return "Free", 0.0
    text = f"${_money(lo)}" if lo == hi else f"${_money(lo)}–${_money(hi)}"
    return text, float(lo)


def _sane(d: Decimal) -> bool:
    """CC-182: a finite price under a million (to the cent or so); anything else isn't a ticket price."""
    return d.is_finite() and d >= 0 and (d == 0 or -4 <= d.adjusted() <= 6)


def _money(d: Decimal) -> str:
    return f"{d:.0f}" if d == d.to_integral_value() else f"{d:.2f}"


def _text(v) -> str:
    return html.unescape(str(v or "")).strip()


def events_in(page_html: str, page_url: str, source_kind: str) -> list[FoundEvent]:
    kind, category = BY_SOURCE.get(source_kind, ("other", None))
    raw = []
    for data in _blocks(page_html):
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


def _address(addr) -> tuple[str, str, str]:
    """CC-218: (street, city, ZIP) of a schema.org address, which is a PostalAddress or plain text. Some pages put the
    whole address in the street field, so that's read like plain text ("78 E Washington St, Chicago, IL 60602")."""
    addr = addr[0] if isinstance(addr, list) and addr else addr
    if isinstance(addr, dict):
        line, city, zip_code = _text(addr.get("streetAddress"))[:MAX_STREET], _text(addr.get("addressLocality")), _text(addr.get("postalCode"))
    elif isinstance(addr, str):
        line, city, zip_code = _text(addr)[:MAX_STREET], "", ""
    else:
        return "", "", ""
    if m := WHOLE_ADDRESS.fullmatch(line):
        return m.group(1), city or m.group(2), zip_code[:10] or m.group(3) or ""
    return line.split(",")[0] if isinstance(addr, str) else line, city[:120], zip_code[:10]


def _place(location) -> tuple[str, str, tuple[str, str, str]]:
    """(name, town, (street, city, ZIP)) of an event's location: the first Place, with its address."""
    for loc in location if isinstance(location, list) else [location]:
        if isinstance(loc, dict) and _text(loc.get("name")):
            addr = loc.get("address")
            town = _text(addr.get("addressLocality"))[:120] if isinstance(addr, dict) else ""
            return _text(loc.get("name"))[:200], town, _address(addr)
    return "", "", ("", "", "")


def event_addresses(page_html: str) -> list[tuple[str, str, str, str]]:
    """CC-218: (location name, street, city, ZIP) for each different place the page's events give a street for, in page order."""
    found = {}
    for data in _blocks(page_html):
        for o in _objects(data):
            if _is_event(o):
                name, _, address = _place(o.get("location"))
                if address[0]:
                    found[(name, *address)] = True
    return list(found)


# CC-222: the schema.org types a venue's site describes itself with. Anything else is left to Claude, which costs a
# fraction of a cent, rather than filling the form with a person's or a product's address.
VENUE_TYPES = {"Organization", "LocalBusiness", "Place", "PerformingArtsTheater", "MovieTheater", "Museum", "MusicVenue",
               "ArtGallery", "EventVenue", "CivicStructure", "TouristAttraction", "LandmarksOrHistoricalBuildings",
               "EntertainmentBusiness", "ComedyClub", "NightClub", "BarOrPub", "Restaurant", "Brewery", "Winery",
               "PerformingGroup", "TheaterGroup", "DanceGroup", "MusicGroup", "NGO", "Library", "PlaceOfWorship", "Church",
               "EducationalOrganization", "CollegeOrUniversity", "StadiumOrArena", "Park", "Zoo", "Aquarium"}
VENUE_NESTED = ("publisher", "mainEntity", "about", "location", "department", "parentOrganization")
STATE_IN_TEXT = re.compile(r",\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:,\s*(?:US|USA))?\s*$")


def _is_venue(obj: dict) -> bool:
    types = obj.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.rsplit("/", 1)[-1] in VENUE_TYPES for t in types)


def _venues(data):
    """The venues in a ld+json block, including ones inside a WebPage or an Organization, top level first. An event's
    location isn't one of them: event_addresses reads that."""
    todo = deque([data])
    while todo:
        for o in _objects(todo.popleft()):
            if not _is_event(o):
                todo.extend(o[key] for key in VENUE_NESTED if key in o)
                if _is_venue(o):
                    yield o


def _state(addr) -> str:
    """The two-letter state of a schema.org address, if it gives one."""
    addr = addr[0] if isinstance(addr, list) and addr else addr
    if isinstance(addr, dict):
        region = _text(addr.get("addressRegion"))
        if region.lower() == "illinois":
            return "IL"
        if re.fullmatch(r"[A-Za-z]{2}", region):
            return region.upper()
        addr = addr.get("streetAddress")
    m = STATE_IN_TEXT.search(_text(addr)[:MAX_STREET]) if isinstance(addr, str) else None
    return m.group(1) if m else ""


def venue_in(page_html: str) -> dict[str, str]:
    """CC-222: what a page's structured data says about its own venue: name, street, city, state and zip_code, each
    "" if it doesn't say. The first venue with a name and a street wins. Otherwise the name and street may come from
    two objects (a theater company and its hall), and a page all of whose events are in one place gives that place."""
    places = []
    for data in _blocks(page_html):
        for o in _venues(data):
            places.append((_text(o.get("name"))[:200], _address(o.get("address")), _state(o.get("address"))))
    whole = next((p for p in places if p[0] and p[1][0]), None)
    name = whole[0] if whole else next((p[0] for p in places if p[0]), "")
    located = whole or next((p for p in places if p[1][0]), None)
    if located:
        (street, city, zip_code), state = located[1], located[2]
    else:
        street = city = zip_code = state = ""
        streets = event_addresses(page_html)
        if len({s[1] for s in streets}) == 1:
            place, street, city, zip_code = streets[0]
            name = name or place
    return {"name": name, "street": street, "city": city, "state": state, "zip_code": zip_code}


def event_places(page_html: str) -> dict[str, dict]:
    """By normalized event name: its venue name, town, category (from its schema.org type) and whether the page gives
    a closing date (events_in takes the opening date for a missing one, which a member's link shouldn't)."""
    found = {}
    for data in _blocks(page_html):
        for o in _objects(data):
            if not (_is_event(o) and _text(o.get("name"))):
                continue
            types = o.get("@type") if isinstance(o.get("@type"), list) else [o.get("@type")]
            category = next((BY_EVENT_TYPE[t] for t in types if t in BY_EVENT_TYPE), None)
            venue, town, _ = _place(o.get("location"))
            found.setdefault(norm_title(_text(o.get("name"))[:300]), {"venue": venue, "town": town, "category": category,
                                                                       "end_given": bool(_when(o.get("endDate"))[0])})
    return found


# CC-134: the page's own title, which a visitor sees, against its event data, which can be out of date (Goodman's
# "A Christmas Carol 2026" page calls its event "A Christmas Carol 2027")
TITLE_OPEN, TITLE_CLOSE = re.compile("<title", re.I), re.compile("</title", re.I)
META, OG_TITLE = re.compile("<meta", re.I), re.compile(r'(?:property|name)=["\']og:title["\']', re.I)
MAX_TITLES, TITLE_CHARS = 10, 300  # CC-182: the page's few titles, each compared at the length a title is kept
CONTENT = re.compile(r'content=["\']([^"\']*)["\']', re.I)
SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]*$")  # " - Goodman Theatre"
SAME_SHOW = 0.8  # how alike the two must be: a different year or a typo, not a list page's own title


def page_titles(page_html: str) -> list[str]:
    """The page's <title>, og:title, WebPage name and last breadcrumb, as written."""
    found = [content for _, content in _tags(page_html or "", TITLE_OPEN, TITLE_CLOSE)][:1]
    found += [c.group(1) for tag, _ in _tags(page_html or "", META) if OG_TITLE.search(tag) and (c := CONTENT.search(tag))]
    for data in _blocks(page_html):
        for o in _objects(data):
            if o.get("@type") == "WebPage":
                found.append(o.get("name"))
            elif o.get("@type") == "BreadcrumbList" and isinstance(o.get("itemListElement"), list) and o["itemListElement"]:
                last = o["itemListElement"][-1]
                if isinstance(last, dict):
                    item = last.get("item")
                    found.append(last.get("name") or (item.get("name") if isinstance(item, dict) else None))
    return [t for t in (_text(f[:5 * TITLE_CHARS])[:TITLE_CHARS] for f in found if isinstance(f, str)) if t][:MAX_TITLES]


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
