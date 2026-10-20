"""Loading seed/events.json, the old Google Sheet's saved events, into a database (local development
and tests). The site stopped reading the sheet in October 2026 (CC-72); its events are ordinary events
now, and their sheet ids (source_id) are kept so old share links still work.

- Events from the file (they have a source_id) are added as approved, or updated in place, matched
  on that id. Events added through the site (no source_id) are never touched.
- An admin's decision stands: a rejected event gets its details updated but stays rejected.
- A row whose ID matches no event, but whose title, venue and start date match an event no other
  row claims, keeps that event (Sheets turned ids like 038656 into numbers).
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .areas import region_of
from .models import Area, Event, EventStatus, Venue

AREAS = {"City": Area.city, "Suburbs": Area.suburbs, "Out of town": Area.out_of_town}


@dataclass
class SyncCounts:
    rows_read: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    venues_added: int = 0
    id_mismatches: int = 0  # rows kept on their event through the computed id (see the module notes)


def _date(s):
    return date.fromisoformat(s) if s else None


def apply_rows(db: Session, rows: list[dict]) -> SyncCounts:
    """Add and update events from rows shaped like seed/events.json. Does not commit."""
    counts = SyncCounts(rows_read=len(rows))
    venues = {(v.name, v.neighborhood): v for v in db.scalars(select(Venue))}
    existing = {e.source_id: e for e in db.scalars(select(Event).where(Event.source_id.is_not(None)))}
    claimed = {row["id"] for row in rows}
    rows = [dict(row) for row in rows]  # ids may be corrected below; leave the caller's rows alone
    for row in rows:
        computed = row.get("computedId")
        if row["id"] not in existing and computed in existing and computed not in claimed:
            row["id"] = computed
            claimed.add(computed)
            counts.id_mismatches += 1

    for row in rows:
        begin, end = _date(row["begin"]), _date(row["end"])
        if begin and end and end < begin:
            counts.skipped += 1
            continue
        key = (row["venue"].strip(), row["hood"].strip())
        venue = venues.get(key)
        if venue is None:
            area = AREAS.get(row["area"], Area.city)
            venue = Venue(name=key[0], neighborhood=key[1], area=area, region=region_of(key[1], area))
            db.add(venue)
            db.flush()
            venues[key] = venue
            counts.venues_added += 1
        fields = dict(
            title=row["title"], venue_id=venue.id, begin_date=begin, end_date=end, open_run=row["openRun"],
            cost_text=row["cost"] or "", category=row["type"], url=row["url"] or "",
            min_price=None if row["min"] is None else Decimal(str(row["min"])).quantize(Decimal("0.01")),
        )
        event = existing.get(row["id"])
        if event is None:
            db.add(Event(source_id=row["id"], status=EventStatus.approved, **fields))
            counts.added += 1
            continue
        changed = {k: v for k, v in fields.items() if getattr(event, k) != v}
        for k, v in changed.items():
            setattr(event, k, v)
        if changed:
            counts.updated += 1
        else:
            counts.unchanged += 1

    return counts
