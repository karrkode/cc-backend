"""CC-301: read-only report on the Show Run migration (0060). Changes nothing.

    python -m scripts.show_run_report                      (local)
    docker exec api python -m scripts.show_run_report      (production, through SSM: ask the owner first)

Before the migration (event_run_dates has no `origin` column yet): what it will copy and what it will leave.
After it: the admin's worklist: live events whose opening or closing date differs from their first or last show.
"""
import re
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import Date, Integer, String, column, func, inspect, select, table

from app.db import SessionLocal
from app.models import Event
from app.visibility import live_condition

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
runs = table("event_run_dates", column("event_id", Integer), column("date", Date), column("time", String), column("origin", String))


def normalise(performances) -> tuple[list[tuple[date, str]], int]:
    """The same reading as migration 0060: the distinct (day, time) shows, and how many entries had no readable date."""
    if not isinstance(performances, list):
        return [], 0
    seen, bad = set(), 0
    for p in performances:
        raw = p.get("date") if isinstance(p, dict) else None
        try:
            day = date.fromisoformat(raw) if isinstance(raw, str) and _DAY.match(raw) else None
        except ValueError:
            day = None
        if day is None:
            bad += 1
            continue
        t = p.get("time")
        seen.add((day, t if isinstance(t, str) and _TIME.match(t) else ""))
    timed = {d for d, t in seen if t}
    return sorted((d, t) for d, t in seen if t or d not in timed), bad


def _span(a, b) -> str:
    f = lambda d: d.strftime("%b %-d, %Y") if d else "none"  # noqa: E731
    return f"{f(a)} to {f(b)}"


def before(db) -> list[str]:
    have = {r[0] for r in db.execute(select(runs.c.event_id).distinct())}
    copy = skipped_fixed = shows = untimed = bad = open_runs = art = 0
    two_a_day = 0
    for e in db.execute(select(Event.id, Event.performances, Event.open_run, Event.category)):
        if not e.performances:
            continue
        if e.id in have:
            skipped_fixed += 1
            continue
        rows, b = normalise(e.performances)
        bad += b
        if not rows:
            continue
        copy += 1
        shows += len(rows)
        untimed += sum(1 for _, t in rows if not t)
        two_a_day += sum(1 for n in Counter(d for d, _ in rows).values() if n > 1)
        open_runs += bool(e.open_run)
        art += e.category == "Visual Art"
    return [f"Events whose dates will be copied: {copy} ({shows} shows)",
            f"  shows with no time: {untimed}", f"  days with 2 or more shows: {two_a_day}",
            f"  entries with no readable date (skipped): {bad}",
            f"  of these events, open runs: {open_runs}, Visual Art: {art}",
            f"Events skipped because they already have a fixed run: {skipped_fixed}"]


def after(db) -> list[str]:
    span = {r.event_id: (r.first, r.last) for r in db.execute(
        select(runs.c.event_id, func.min(runs.c.date).label("first"), func.max(runs.c.date).label("last"))
        .group_by(runs.c.event_id))}
    by_origin = dict(db.execute(select(runs.c.origin, func.count()).group_by(runs.c.origin)).all())
    out = []
    for e in db.execute(select(Event.id, Event.title, Event.begin_date, Event.end_date).where(live_condition())
                        .order_by(Event.id)):
        first, last = span.get(e.id, (None, None))
        if first is not None and (e.begin_date != first or e.end_date != last):
            out.append(f"  {e.id}  {e.title}: listed {_span(e.begin_date, e.end_date)}; shows {_span(first, last)}")
    return [f"Shows by origin: {by_origin or 'none'}",
            "Live events whose opening or closing differs from their first or last show:", *(out or ["  none"])]


def report(db) -> list[str]:
    if "origin" in {c["name"] for c in inspect(db.get_bind()).get_columns("event_run_dates")}:
        return ["Show Run migration: applied (0060)", *after(db)]
    return ["Show Run migration: not applied yet", *before(db)]


def main():
    with SessionLocal() as db:
        print("\n".join(report(db)))


if __name__ == "__main__":
    main()
