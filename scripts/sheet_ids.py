"""Fill in the Google Sheet's ID column, so editing a title, venue or date keeps the same event.

    python -m scripts.sheet_ids

Reads the sheet (nothing is changed anywhere) and prints:
- whether the sheet has an ID column yet, and which rows are missing an ID
- a column to paste into the ID column, one line per sheet row from the first row under
  the header: each row's current id (the one already in the database), blank for empty rows

Pasting those exact values matters: they're the ids events already have, which the site's
share links and people's "I've been" marks are tied to. Re-run it whenever rows are added
without an ID, and fill in just the blank ones.
"""
from app.sheet import fetch_sheet_html, parse_sheet, row_id


def main():
    rows = parse_sheet(fetch_sheet_html(), keep_blank=True)
    has_column = any(r["id"] for r in rows)
    events = [r for r in rows if r["event"]]
    missing = [r for r in events if not row_id(r)[1]]
    first = rows[0]["row"] if rows else None

    print(f"ID column: {'found' if has_column else 'not found (add a column headed ID)'}. "
          f"{len(events)} events, {len(missing)} without an ID.")
    if has_column and missing:
        print("Rows missing an ID: " + ", ".join(str(r["row"]) for r in missing if r["row"]))
    if not missing:
        print("Nothing to fill in.")
        return
    print(f"\nPaste the lines below into the ID column starting at row {first} (keep cells that already have an ID):\n")
    for r in rows:
        print(row_id(r)[0] if r["event"] else "")


if __name__ == "__main__":
    main()
