"""CC-318: a one-off catch-up that gives every item on the Event Sourcing review list a proposed picture, the way
CC-310 does for new ones (app/discovery/pictures.py: its own page, else up to two tickets or calendar links on it).

    python -m scripts.backfill_item_pictures                  dry run (the default): counts only, nothing fetched or changed
    python -m scripts.backfill_item_pictures --apply          the real run
    ... --apply --limit 25                                    only the first 25 items that need a look
    ... --retry-missing                                       also look again where the last look gave a reason

- A proposal only: the picture sits on the review item, and goes live only if an admin approves the item. Nothing on a
  live event changes, and an admin's own upload or removal is never replaced.
- Safe to run again: an item with a picture, an admin's choice or a note (a reason, or a try still going) is skipped,
  so a second run has nothing to do. The hourly run finishes the tries of items whose look hit a fetch error.
- Safe next to the hourly run and the admins: each item is read afresh just before its look, and skipped if it changed.
  Pick --limit so a run ends before the next hourly run.
- Polite: every fetch is app/discovery/fetch.py's (public hosts, robots.txt, the gap between requests to one site). No
  Claude call is made, so it costs nothing against the discovery budget.
- The dry run also says how many live sourced events still have no picture. This job doesn't touch them (CC-246's
  hourly run does).
The real run on production is the product owner's; paste the summary (it names no private links) on CC-318.
"""
import argparse
from collections import Counter

from sqlalchemy import func, select

from app.db import SessionLocal
from app.discovery import pictures
from app.models import Event, EventStatus
from app.clock import utcnow


def report(db, retry_missing: bool = False) -> dict:
    groups = pictures.backlog(db, retry_missing)
    counts = {k: len(v) for k, v in groups.items()}
    counts["found"] = sum(counts.values())
    counts["live_without_picture"] = db.scalar(select(func.count(Event.id)).where(  # the ones CC-246's hourly run will look at
        Event.found_by_id.is_not(None), Event.status == EventStatus.approved, Event.image_key.is_(None), Event.url != "",
        Event.picture_tries < pictures.MAX_TRIES, Event.image_override.is_(False))) or 0
    return counts


def summary(counts: dict, results: list[tuple[str, str, str]] | None, dry_run: bool) -> str:
    lines = [("Dry run: " if dry_run else "Run: ") + f"{counts['found']} items on the review list.",
             f"- already have a picture: {counts['has_picture']}",
             f"- admin's own choice, left alone: {counts['admin_choice']}",
             f"- skipped, a note is recorded (a reason, or a try still going): {counts['reason']}",
             f"- to look at{' (would be)' if dry_run else ''}: {counts['todo']}",
             f"- live sourced events still waiting for the hourly picture look (not touched here): {counts['live_without_picture']}"]
    if results is not None:
        by = Counter(status for _, status, _ in results)
        lines += [f"Result: {by['ok']} pictures proposed, {by['none']} with no picture (reason recorded), "
                  f"{by['error']} errors (the hourly run tries again while an item has tries left)."]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="really fetch and store (without it: a dry run)")
    parser.add_argument("--retry-missing", action="store_true", help="look again where the last look gave a reason")
    parser.add_argument("--limit", type=int, help="look at no more than this many items")
    args = parser.parse_args()
    with SessionLocal() as db:
        counts = report(db, args.retry_missing)
        if args.limit is not None:
            counts["todo"] = min(counts["todo"], args.limit)
        if not args.apply:
            print(summary(counts, None, True))
            return
        results = pictures.backfill_items(db, args.retry_missing, args.limit, utcnow(),
                                          on_result=lambda r: print(f"{r[1]:5} {r[0]}: {r[2]}", flush=True))
        print(summary(counts, results, False))


if __name__ == "__main__":
    main()
