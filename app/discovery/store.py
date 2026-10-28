"""The holding list: put what a check found into DiscoveredItem.

- A new event becomes a "new" item, for review (CC-71).
- One already in the catalog becomes "listed" (matched by its page address, or by title at the
  source's venue), so the sheet's events aren't proposed twice.
- When something already listed has changed (a price added, dates moved), a proposed update is made
  ("update", with the changes), for an admin to accept or dismiss. Nothing is changed by itself.
- An item seen again just has its details refreshed (unless an admin has already decided on it).
- A new item matching one of the admin's skip rules (CC-71) is rejected quietly, and the rule counts it.
- CC-79: an item that is a catalog event (listed or approved) passes its performance dates and times on
  to that event, for the planner. That's the one thing discovery changes on an event by itself: the
  dates aren't shown on cards, and admins don't edit them.
"""
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from ..categories import BY_SOURCE_KIND, REMOVED
from ..models import DiscoveredItem, DiscoverySource, Event, SkipRule, Venue
from .readers import FoundEvent
from .rules import norm_title

DECIDED = {"approved", "rejected"}


def _date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _price(v) -> Decimal | None:
    try:
        return None if v is None else Decimal(str(v)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def key_for(e: FoundEvent) -> str:
    return (e.url or "").split("#")[0].rstrip("/")[:500] or "title:" + norm_title(e.title)[:490]


def fields_of(e: FoundEvent) -> dict:
    return {"title": e.title.strip()[:300], "url": (e.url or "")[:2000], "kind": e.kind, "category": e.category,
            "venue_name": (e.venue_name or "")[:200], "begin_date": _date(e.begin_date), "end_date": _date(e.end_date),
            "open_run": e.open_run, "cost_text": (e.cost_text or "")[:120], "min_price": _price(e.min_price),
            "performances": [p.model_dump() for p in e.performances][:400], "weekly": weekly_of(e), "weeks": weeks_of(e)}


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _hhmm(t: str | None) -> str | None:
    try:
        h, m = (t or "").strip().split(":")[:2]
        h, m = int(h), int(m)
    except ValueError:
        return None
    return f"{h:02d}:{m:02d}" if 0 <= h < 24 and 0 <= m < 60 else None


def weekly_of(e: FoundEvent) -> dict:
    """CC-88: the show days the reader gave, as an event's weekly pattern: {"thu": ["20:00"]}, days in order."""
    got = {}
    for d in e.show_days:
        times = sorted({t for t in map(_hhmm, d.times) if t})[:6]
        if times:
            got[d.weekday] = sorted(set(got.get(d.weekday, [])) | set(times))[:6]
    return {d: got[d] for d in DAYS if d in got}


def weeks_of(e: FoundEvent) -> dict:
    """CC-88: the show days that are on some weeks of the month only, {"thu": [1, 3]}; every week is left out."""
    weekly = weekly_of(e)
    got = {d.weekday: sorted({w for w in d.weeks if 1 <= w <= 5}) for d in e.show_days if d.weekday in weekly and d.weeks}
    return {d: got[d] for d in DAYS if got.get(d)}


KEEP_IF_MISSING = ("begin_date", "end_date", "cost_text", "min_price", "performances", "weekly")


def _refresh(item: DiscoveredItem, f: dict) -> None:
    """Take the source's latest details, but a reading that leaves something out (an event's own page
    without the price the list gave) doesn't erase what was already found (CC-88)."""
    for k, v in f.items():
        if k in KEEP_IF_MISSING and v in (None, "", [], {}) and getattr(item, k) not in (None, "", [], {}):
            continue
        if k == "weeks" and not f["weekly"] and item.weekly:  # the show days were kept, so their weeks are too
            continue
        setattr(item, k, v)


def find_listed(db, source: DiscoverySource, f: dict) -> Event | None:
    """The catalog event this is, if any: the same page address, or the same title (ignoring
    punctuation and capitals) at the source's venue, or at a venue with the same name ("The Art
    Institute of Chicago" was entered twice)."""
    if f["url"]:
        hit = db.scalar(select(Event).where(func.lower(Event.url) == f["url"].lower()))
        if hit:
            return hit
    title = norm_title(f["title"])
    venue_ids = venue_ids_of(db, source)
    if not venue_ids:  # a source with no venue: the title alone has to do
        return next((ev for ev in db.scalars(select(Event).where(func.lower(Event.title) == f["title"].lower()))), None)
    return next((ev for ev in db.scalars(select(Event).where(Event.venue_id.in_(venue_ids))) if norm_title(ev.title) == title), None)


def venue_ids_of(db, source: DiscoverySource | None) -> set[int]:
    """The source's venue, and any other venue with the same name (with or without "The")."""
    if source is None:  # CC-133: its source was deleted
        return set()
    names = {_place(source.venue_name), _place(source.venue.name if source.venue else "")} - {""}
    return {source.venue_id} - {None} | {v.id for v in db.scalars(select(Venue)) if _place(v.name) in names}


def venue_for(db, source: DiscoverySource | None, item: DiscoveredItem) -> Venue | None:
    """The venue to file an item under: the source's own venue unless the item names another (the
    CSO plays in Wheaton too), then a known venue with that name. None: an admin picks or adds it.
    CC-133: an item whose source was deleted goes by the venue it names."""
    named = _place(item.venue_name)
    if source is None:
        return next((v for v in db.scalars(select(Venue).order_by(Venue.id)) if named and _place(v.name) == named), None)
    if not named or named in {_place(source.venue_name), _place(source.venue.name if source.venue else "")}:
        return source.venue
    return next((v for v in db.scalars(select(Venue).order_by(Venue.id)) if _place(v.name) == named), None)


def matching_rule(db, source_id: int, title: str) -> SkipRule | None:
    words = f" {norm_title(title)} "
    return next((r for r in db.scalars(select(SkipRule).where((SkipRule.source_id == source_id) | SkipRule.source_id.is_(None))
                                       .order_by(SkipRule.id)) if f" {r.phrase} " in words), None)


def rule_reason(rule: SkipRule) -> str:
    return f"skip rule: \u201c{rule.phrase}\u201d"[:200]


def _similar(a: str, b: str) -> bool:
    """Titles close enough to ask "is this the same event?": one contains the other, or most words are shared."""
    if not a or not b:
        return False
    if f" {a} " in f" {b} " or f" {b} " in f" {a} ":
        return True
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) / len(wa | wb) >= 0.6


def possible_duplicates(db, items: list[DiscoveredItem]) -> dict[int, Event]:
    """For new items: a catalog event at the same venue with a similar title (not the same, or it would be "listed")."""
    out, by_venue = {}, {}
    for item in items:
        if item.status != "new":
            continue
        ids = frozenset(venue_ids_of(db, item.source))
        if not ids:
            continue
        if ids not in by_venue:
            by_venue[ids] = [(norm_title(e.title), e) for e in db.scalars(select(Event).where(Event.venue_id.in_(ids)))]
        title = norm_title(item.title)
        hit = next((e for t, e in by_venue[ids] if _similar(title, t)), None)
        if hit is not None:
            out[item.id] = hit
    return out


def _place(name: str) -> str:
    n = norm_title(name or "")
    return n[4:] if n.startswith("the ") else n


def changes_against(event: Event, f: dict) -> dict:
    """What the source now says differently from the listed event, in the fields it fills in later."""
    out = {}
    if f["cost_text"] and f["cost_text"] != (event.cost_text or ""):
        out["cost_text"] = [event.cost_text or "", f["cost_text"]]
    if f["min_price"] is not None and (event.min_price is None or Decimal(event.min_price) != f["min_price"]):
        out["min_price"] = [None if event.min_price is None else float(event.min_price), float(f["min_price"])]
    for k in ("begin_date", "end_date"):
        if f[k] and f[k] != getattr(event, k):
            out[k] = [None if getattr(event, k) is None else getattr(event, k).isoformat(), f[k].isoformat()]
    if f.get("weekly") and not event.weekly:  # CC-88: show days for an event that has none; an admin's are kept
        out["weekly"] = [None, f["weekly"]]
        if f.get("weeks"):
            out["weeks"] = [None, f["weeks"]]
    return out


def _same_title(db, source: DiscoverySource, found: FoundEvent, key: str):
    """The same event found once with a link and once without (a list page vs. a calendar): one item.
    A linked item takes over an unlinked one's title key; two different links stay two items."""
    title = norm_title(found.title)
    for item in db.scalars(select(DiscoveredItem).where(DiscoveredItem.source_id == source.id)):
        if norm_title(item.title) != title:
            continue
        if item.key.startswith("title:"):
            item.key = key
            return item
        if key.startswith("title:"):
            found.url = item.url  # keep the link we already have
            return item
    return None


def sync_performances(db, item: DiscoveredItem) -> None:
    """CC-79: give the catalog event this item is the dates and times found for it (when any were found)."""
    if not item.event_id or not item.performances:
        return
    event = db.get(Event, item.event_id)
    found = sorted({(p.get("date"), p.get("time")) for p in item.performances if p.get("date")}, key=lambda dt: (dt[0], dt[1] or ""))
    dates = [{"date": d, "time": t} for d, t in found]
    if event is not None and dates and dates != (event.performances or []):
        event.performances = dates


def record(db, source: DiscoverySource, found: FoundEvent, skip: str) -> str:
    """Store one found event. Returns what it was: new | update | skipped | seen."""
    what = _record(db, source, found, skip)
    item = db.scalar(select(DiscoveredItem).where(DiscoveredItem.source_id == source.id, DiscoveredItem.key == key_for(found)))
    if item is not None:
        sync_performances(db, item)
    return what


def _record(db, source: DiscoverySource, found: FoundEvent, skip: str) -> str:
    now = datetime.now(UTC)
    f = fields_of(found)
    key = key_for(found)
    item = db.scalar(select(DiscoveredItem).where(DiscoveredItem.source_id == source.id, DiscoveredItem.key == key)) \
        or _same_title(db, source, found, key)
    f = fields_of(found)
    # CC-125: the source's kind gives the category when the reading didn't; an opera source's events are opera (Music)
    category, opera = BY_SOURCE_KIND.get(source.kind.value, (None, False))
    f["category"], f["opera"] = (category if opera else f["category"] or category), opera
    if f["category"] in REMOVED and not skip:  # CC-133: Comedy and Educational aren't listed
        skip = f"{f['category']}, which isn't listed"
    if item is None:
        item = DiscoveredItem(source_id=source.id, key=key, first_seen_at=now, last_seen_at=now, changes={}, detail_checks=[], **f)
        db.add(item)
        if skip:
            item.status, item.skip_reason = "skipped", skip[:200]
            return "skipped"
        listed = find_listed(db, source, f)
        if listed is None:
            rule = matching_rule(db, source.id, f["title"])
            if rule is not None:
                rule.caught += 1
                item.status, item.skip_reason = "rejected", rule_reason(rule)
                return "skipped"
            item.status = "new"
            return "new"
        item.event_id, item.status = listed.id, "listed"
        diff = changes_against(listed, f)
        if diff:
            item.status, item.changes = "update", diff
            return "update"
        return "seen"
    item.last_seen_at = now
    if item.status in DECIDED or item.status == "skipped":
        if item.status == "approved" and item.event_id:  # live now: offer the source's changes as an update
            listed = db.get(Event, item.event_id)
            diff = changes_against(listed, f) if listed else {}
            if diff and diff != item.changes:
                item.changes, item.status = diff, "update"
                for k, v in f.items():
                    setattr(item, k, v)
                return "update"
        return "seen"
    _refresh(item, f)
    if skip and item.status == "new":  # a closer look (the event's own page) showed it's one to leave out
        item.status, item.skip_reason = "skipped", skip[:200]
        return "skipped"
    if item.status in ("listed", "update") and item.event_id:
        listed = db.get(Event, item.event_id)
        diff = changes_against(listed, f) if listed else {}
        if diff and diff != item.changes:
            item.changes, item.status = diff, "update"
            return "update"
    return "seen"
