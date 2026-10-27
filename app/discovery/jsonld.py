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
from decimal import Decimal, InvalidOperation
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
    time is the venue's local time as written."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?", str(value or ""))
    if not m:
        return None, None
    t = m.group(2)
    return m.group(1), (t if t and t != "00:00" else None)


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
