"""The holding list: put what a check found into DiscoveredItem.

- A new event becomes a "new" item, for review (CC-71).
- One already in the catalog becomes "listed" (matched by its page address, or by title at the
  source's venue), so the sheet's events aren't proposed twice.
- When something already listed has changed (a price added, dates moved), a proposed update is made
  ("update", with the changes), for an admin to accept or dismiss. Nothing is changed by itself.
- An item seen again just has its details refreshed (unless an admin has already decided on it).
"""
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from ..models import DiscoveredItem, DiscoverySource, Event, Venue
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
            "performances": [p.model_dump() for p in e.performances][:400]}


def find_listed(db, source: DiscoverySource, f: dict) -> Event | None:
    """The catalog event this is, if any: the same page address, or the same title (ignoring
    punctuation and capitals) at the source's venue, or at a venue with the same name ("The Art
    Institute of Chicago" was entered twice)."""
    if f["url"]:
        hit = db.scalar(select(Event).where(func.lower(Event.url) == f["url"].lower()))
        if hit:
            return hit
    title = norm_title(f["title"])
    names = {_place(source.venue_name), _place(source.venue.name if source.venue else "")} - {""}
    venue_ids = {source.venue_id} - {None} | {v.id for v in db.scalars(select(Venue)) if _place(v.name) in names}
    if not venue_ids:  # a source with no venue: the title alone has to do
        return next((ev for ev in db.scalars(select(Event).where(func.lower(Event.title) == f["title"].lower()))), None)
    return next((ev for ev in db.scalars(select(Event).where(Event.venue_id.in_(venue_ids))) if norm_title(ev.title) == title), None)


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
    return out


def record(db, source: DiscoverySource, found: FoundEvent, skip: str) -> str:
    """Store one found event. Returns what it was: new | update | skipped | seen."""
    now = datetime.now(UTC)
    f = fields_of(found)
    key = key_for(found)
    item = db.scalar(select(DiscoveredItem).where(DiscoveredItem.source_id == source.id, DiscoveredItem.key == key))
    if item is None:
        item = DiscoveredItem(source_id=source.id, key=key, first_seen_at=now, last_seen_at=now, changes={}, detail_checks=[], **f)
        db.add(item)
        if skip:
            item.status, item.skip_reason = "skipped", skip[:200]
            return "skipped"
        listed = find_listed(db, source, f)
        if listed is None:
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
    for k, v in f.items():
        setattr(item, k, v)
    if item.status in ("listed", "update") and item.event_id:
        listed = db.get(Event, item.event_id)
        diff = changes_against(listed, f) if listed else {}
        if diff and diff != item.changes:
            item.changes, item.status = diff, "update"
            return "update"
    return "seen"
