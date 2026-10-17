"""Load events from seed/events.json (the old Google Sheet site's saved copy) into the database.

    python -m scripts.seed                      # reads seed/events.json
    python -m scripts.seed path/to/events.json

Safe to run again: events are matched on their old id (source_id) and updated in place,
so nothing is duplicated and review statuses already set in the database are kept.
To keep up with the live sheet, use scripts.sync_sheet instead.
"""
import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.db import SessionLocal
from app.sync import apply_rows

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "seed" / "events.json"


def seed(path: Path) -> dict:
    rows = json.loads(path.read_text(encoding="utf-8"))["events"]
    with SessionLocal() as db:
        counts = apply_rows(db, rows, withdraw_missing=False)  # a file is a snapshot: never withdraw
        db.commit()
    return asdict(counts)


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    if not path.exists():
        raise SystemExit(f"Could not find {path}. Pass its path: python -m scripts.seed path/to/events.json")
    c = seed(path)
    print(f"Seeded from {path}: {c['added']} added, {c['updated']} updated, {c['unchanged']} unchanged, "
          f"{c['skipped']} skipped, {c['venues_added']} new venues.")


if __name__ == "__main__":
    main()
