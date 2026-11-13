"""CC-199: CSV cells that a spreadsheet reads as data, never as a formula."""
import csv
import io

_LEAD = ("=", "+", "-", "@", "\t", "\r")


def cell(value) -> str:
    """CC-199: a cell starting with = + - @ tab or CR gets a ' in front, so Excel and Sheets don't run it."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_LEAD) else text


def to_csv(header: list, rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([cell(c) for c in header])
    for row in rows:
        w.writerow([cell(c) for c in row])
    return buf.getvalue()
