"""Bringing the database in line with the Google Sheet (and with seed/events.json).

Rules:
- Events that came from the sheet (they have a source_id) follow the sheet: new rows are
  added as approved, changed rows are updated.
- Events added through the site (no source_id) are never touched.
- An admin's decision stands: a rejected sheet event gets its details updated but stays rejected.
- Nothing is deleted. A sheet event that is no longer on the sheet becomes "withdrawn"
  (hidden, but people's "I've been" marks survive); if it comes back it is approved again.
- A row whose ID matches no event, but whose title, venue and start date match an event no
  other row claims, keeps that event: the ID cell was most likely mangled (Sheets turns
  ids like 038656 or 541e30 into numbers) rather than meant as a new event. Each one is noted.
- If the sheet suddenly has far fewer events than we hold, the run is refused rather than
  withdrawing most of the catalog over what is more likely a read problem than a real change.
"""
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .areas import region_of
from .config import get_settings
from .models import Area, Event, EventStatus, SheetSync, Venue
from .sheet import SheetUnavailable, read_sheet

AREAS = {"City": Area.city, "Suburbs": Area.suburbs, "Out of town": Area.out_of_town}


class SyncRefused(Exception):
    pass


@dataclass
class SyncCounts:
    rows_read: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    withdrawn: int = 0
    restored: int = 0
    skipped: int = 0
    venues_added: int = 0
    id_mismatches: int = 0  # rows kept on their event through the computed id (see the module notes)


def _date(s):
    return date.fromisoformat(s) if s else None


def apply_rows(db: Session, rows: list[dict], *, withdraw_missing: bool) -> SyncCounts:
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
        if event.status == EventStatus.withdrawn:  # back on the sheet
            event.status = EventStatus.approved
            counts.restored += 1
        elif changed:
            counts.updated += 1
        else:
            counts.unchanged += 1

    if withdraw_missing:
        on_sheet = {row["id"] for row in rows}
        for source_id, event in existing.items():
            if source_id not in on_sheet and event.status == EventStatus.approved:
                event.status = EventStatus.withdrawn
                counts.withdrawn += 1
    return counts


def sync_from_sheet(db: Session, *, dry_run: bool = False, fetch=None) -> SheetSync:
    """Read the sheet and apply it. Always records the run in sheet_syncs, even when it fails."""
    run = SheetSync(started_at=datetime.now(UTC), result="failed")
    try:
        rows = read_sheet(fetch) if fetch else read_sheet()
        held = db.scalar(select(func.count(Event.id)).where(Event.source_id.is_not(None),
                                                            Event.status == EventStatus.approved))
        if held and len(rows) < held * get_settings().sync_min_ratio:
            raise SyncRefused(f"The sheet has {len(rows)} events but {held} are live from it. "
                              "Not withdrawing that many; check the sheet, then run the sync again.")
        counts = apply_rows(db, rows, withdraw_missing=True)
        for k, v in asdict(counts).items():
            if hasattr(run, k):
                setattr(run, k, v)
        notes = [f"{counts.venues_added} new venues"] if counts.venues_added else []
        no_id = sum(1 for r in rows if not r.get("idFromSheet"))
        if no_id:  # these rows become new events if their title, venue or start date is edited
            notes.append(f"{no_id} rows have no ID in the sheet (run scripts.sheet_ids)")
        if counts.id_mismatches:
            notes.append(f"{counts.id_mismatches} rows have an ID that matches no event; kept their existing "
                         "event (check those ID cells: Sheets may have turned them into numbers)")
        run.message = "; ".join(notes)
        if dry_run:
            db.rollback()
            run.result = "dry_run"
        else:
            run.result = "ok"
    except (SheetUnavailable, SyncRefused) as e:
        db.rollback()
        run.message = str(e)
    except Exception as e:  # keep a record of unexpected failures too, then let them surface
        db.rollback()
        run.message = f"{type(e).__name__}: {e}"
        run.finished_at = datetime.now(UTC)
        db.add(run)
        db.commit()
        raise
    run.finished_at = datetime.now(UTC)
    db.add(run)
    db.commit()  # the event changes (unless rolled back above) and the log row together
    return run
