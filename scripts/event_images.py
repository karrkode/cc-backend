"""The first event photos (CC-250): a one-off import in two steps.

1. upload: on a computer that has the photos. Reads a reviewed mapping (CSV: event_id,file,source_url), makes each
   photo's WebP (app/event_images.py), stores the original and the WebP, and writes a manifest of event ids and keys.
   It never touches the database. To store them in production, run it with the bucket's credentials:

       AWS_PROFILE=culture-crew EVENT_IMAGE_BACKEND=s3 .venv/bin/python -m scripts.event_images upload \\
           mapping.csv "<photo folder>" --out seed/event_images_cc250.json

2. apply: where the database is (the API container). Gives each event in the manifest its photo, only where the
   event exists and has none yet, so it's safe to run again and never replaces or removes anything.

       python -m scripts.event_images apply seed/event_images_cc250.json [--dry-run]
"""
import argparse
import csv
import json
from pathlib import Path

from sqlalchemy import select

from app import event_images


def upload(mapping: Path, folder: Path, out: Path) -> list[dict]:
    rows = list(csv.DictReader(mapping.open(newline="", encoding="utf-8")))
    ids = [r["event_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("The mapping gives an event more than one photo. Fix it first.")
    to, manifest = event_images.store(), []
    for i, r in enumerate(rows, 1):
        path = folder / r["file"]
        key = event_images.save(int(r["event_id"]), path.read_bytes(), path.suffix, to)
        manifest.append({"event_id": int(r["event_id"]), "key": key, "source_url": r.get("source_url", ""),
                         "file": r["file"]})
        print(f"{i}/{len(rows)} {r['event_id']} {key}", flush=True)
    out.write_text(json.dumps(sorted(manifest, key=lambda m: m["event_id"]), indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {out} ({len(manifest)} photos).")
    return manifest


def apply(manifest: Path, dry_run: bool = False, db=None) -> dict:
    from app.db import SessionLocal
    from app.models import Event

    entries = json.loads(manifest.read_text(encoding="utf-8"))
    counts = {"set": 0, "has_one": 0, "no_event": 0}
    own = db is None
    db = db or SessionLocal()
    try:
        events = {e.id: e for e in db.scalars(select(Event).where(Event.id.in_([m["event_id"] for m in entries])))}
        for m in entries:
            e = events.get(m["event_id"])
            if e is None:
                counts["no_event"] += 1
                print(f"skip {m['event_id']}: no such event")
            elif e.image_key:
                counts["has_one"] += 1  # CC-250: never replace a photo someone has set since
            else:
                e.image_key, e.image_source_url = m["key"], m.get("source_url", "")
                counts["set"] += 1
        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        if own:
            db.close()
    print(("Would set" if dry_run else "Set") + f" {counts['set']}; {counts['has_one']} already had a photo; "
          f"{counts['no_event']} not found.")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload", help="make and store the photos; write the manifest")
    up.add_argument("mapping", type=Path)
    up.add_argument("folder", type=Path)
    up.add_argument("--out", type=Path, required=True)
    ap = sub.add_parser("apply", help="give the events in the manifest their photos")
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.cmd == "upload":
        upload(args.mapping, args.folder, args.out)
    else:
        apply(args.manifest, args.dry_run)


if __name__ == "__main__":
    main()
