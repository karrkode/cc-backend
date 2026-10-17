"""Load events from the old site's data/events.json into the database.

    python -m scripts.seed                      # reads /data/events.json (Docker) or ../data/events.json
    python -m scripts.seed path/to/events.json

Safe to run again: events are matched on their old id (source_id) and updated in place,
so nothing is duplicated and review statuses already set in the database are kept.
"""
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Area, Event, EventStatus, Venue

AREAS = {"City": Area.city, "Suburbs": Area.suburbs, "Out of town": Area.out_of_town}
DEFAULT_PATHS = [Path("/data/events.json"), Path(__file__).resolve().parents[2] / "data" / "events.json"]


def _date(s):
    return date.fromisoformat(s) if s else None


def seed(path: Path) -> dict:
    events = json.loads(path.read_text(encoding="utf-8"))["events"]
    counts = {"added": 0, "updated": 0, "skipped": 0, "venues_added": 0}
    with SessionLocal() as db:
        venues = {(v.name, v.neighborhood): v for v in db.scalars(select(Venue))}
        existing = {e.source_id: e for e in db.scalars(select(Event).where(Event.source_id.is_not(None)))}
        for row in events:
            begin, end = _date(row["begin"]), _date(row["end"])
            if begin and end and end < begin:
                print(f"  skipped {row['title']!r}: ends before it begins")
                counts["skipped"] += 1
                continue
            key = (row["venue"].strip(), row["hood"].strip())
            venue = venues.get(key)
            if venue is None:
                venue = Venue(name=key[0], neighborhood=key[1], area=AREAS.get(row["area"], Area.city))
                db.add(venue)
                venues[key] = venue
                counts["venues_added"] += 1
            fields = dict(
                title=row["title"], venue=venue, begin_date=begin, end_date=end, open_run=row["openRun"],
                cost_text=row["cost"] or "", category=row["type"], url=row["url"] or "",
                min_price=None if row["min"] is None else Decimal(str(row["min"])),
            )
            event = existing.get(row["id"])
            if event is None:
                db.add(Event(source_id=row["id"], status=EventStatus.approved, **fields))
                counts["added"] += 1
            else:
                for k, v in fields.items():
                    setattr(event, k, v)
                counts["updated"] += 1
        db.commit()
    return counts


def main():
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = next((p for p in DEFAULT_PATHS if p.exists()), None)
        if path is None:
            raise SystemExit("Could not find events.json. Pass its path: python -m scripts.seed path/to/events.json")
    counts = seed(path)
    print(f"Seeded from {path}: {counts['added']} added, {counts['updated']} updated, "
          f"{counts['skipped']} skipped, {counts['venues_added']} new venues.")


if __name__ == "__main__":
    main()
