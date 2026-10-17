"""Bring the database in line with the Google Sheet. Meant to run nightly.

    python -m scripts.sync_sheet              # read the sheet and apply the changes
    python -m scripts.sync_sheet --dry-run    # show what would change, change nothing

Every run is recorded in the sheet_syncs table (GET /admin/syncs). Exits with status 1
when the run fails, so the scheduler and monitoring can notice.
"""
import argparse
import sys

from app.db import SessionLocal
from app.sync import sync_from_sheet


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would change without changing it")
    args = parser.parse_args()

    with SessionLocal() as db:
        run = sync_from_sheet(db, dry_run=args.dry_run)
    summary = (f"{run.rows_read} rows read: {run.added} added, {run.updated} updated, {run.unchanged} unchanged, "
               f"{run.withdrawn} withdrawn, {run.restored} restored, {run.skipped} skipped")
    if run.result == "failed":
        print(f"Sheet sync FAILED: {run.message}", file=sys.stderr)
        sys.exit(1)
    prefix = "Dry run (nothing changed)" if run.result == "dry_run" else "Sheet sync done"
    print(f"{prefix}. {summary}." + (f" {run.message}." if run.message else ""))


if __name__ == "__main__":
    main()
