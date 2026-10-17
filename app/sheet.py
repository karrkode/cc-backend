"""Reading the Culture Crew Google Sheet.

Moved from the old site's app.py. It reads the sheet's public HTML view (no Google
sign-in or API key), finds the header row, and turns each row into an event dict in
the same shape as seed/events.json, so the seed and the nightly sync share one path.
"""
import hashlib
import html
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

from .areas import area_of
from .config import get_settings
from .models import Area


class SheetUnavailable(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect means Google wants a sign-in
        return None


def sheet_url() -> str:
    s = get_settings()
    return f"https://docs.google.com/spreadsheets/d/{s.sheet_id}/htmlview/sheet?headers=true&gid={s.sheet_gid}"


def fetch_sheet_html() -> str:
    req = urllib.request.Request(sheet_url(), headers={"User-Agent": "Mozilla/5.0 (culture-crew-sync)"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=get_settings().sheet_timeout_seconds) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 401, 403):
            raise SheetUnavailable('The sheet is private. Share it as "Anyone with the link can view".')
        raise SheetUnavailable(f"Google answered with HTTP {e.code}.")
    except Exception as e:  # offline, DNS, timeout
        raise SheetUnavailable(f"The sheet could not be reached ({type(e).__name__}).")


# ---- parsing the HTML view ---------------------------------------------------

def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).replace("\xa0", " ").strip()


def _link(fragment: str) -> str:
    m = re.search(r'<a[^>]*href="([^"]*)"', fragment)
    if not m:
        return ""
    href = html.unescape(m.group(1))
    u = urllib.parse.urlparse(href)
    if u.netloc.endswith("google.com") and u.path == "/url":  # Google wraps outbound links
        q = urllib.parse.parse_qs(u.query).get("q")
        if q:
            href = q[0]
    return href if re.match(r"https?://", href, re.I) else ""


WANTED = {"event": "Event", "venue": "Venue", "begin": "Begin Date", "end": "End",
          "hood": "Neighborhood", "cost": "Entry Cost", "category": "Category",
          "id": "ID"}  # optional: keeps an event the same event when its title, venue or date is edited


def parse_sheet(page: str, keep_blank: bool = False) -> list[dict]:
    """Rows of the sheet as dicts keyed event/venue/begin/end/hood/cost/category/id/url/row.

    row is the sheet's own row number (from the htmlview's row headers), None if not shown.
    keep_blank keeps rows with no Event too, so every row below the header is accounted for.
    """
    cols, rows = None, []
    for chunk in re.split(r"<tr[\s>]", page)[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", chunk, flags=re.S)
        if not cells:
            continue
        texts = [_text(c) for c in cells]
        if cols is None:
            if "Event" in texts and "Venue" in texts:  # the header row
                cols = {key: texts.index(label) for key, label in WANTED.items() if label in texts}
            continue

        def get(key):
            return texts[cols[key]] if key in cols and cols[key] < len(texts) else ""

        if not get("event") and not keep_blank:
            continue
        row = {key: get(key) for key in WANTED}
        row["url"] = _link(cells[cols["event"]]) if cols["event"] < len(cells) else ""
        number = re.search(r"<th[^>]*>(.*?)</th>", chunk, flags=re.S)
        row["row"] = int(_text(number.group(1))) if number and _text(number.group(1)).isdigit() else None
        rows.append(row)
    if cols is None:
        raise SheetUnavailable("The sheet opened, but no header row with Event and Venue was found.")
    if not rows:
        raise SheetUnavailable("The sheet opened but had no event rows.")
    return rows


# ---- sheet rows -> events ----------------------------------------------------

AREA_LABELS = {Area.city: "City", Area.suburbs: "Suburbs", Area.out_of_town: "Out of town"}
CONCERT = [r"^Candlelight:", r"Concert Tour", r"In Concert$", r"Trans-Siberian Orchestra"]
COMEDY_TITLES = {"Improv Supernova", "Pandemonium, Please Hold", "Reality Detox: The Improv Experience",
                 "Brad Williams: The Tall Tales Tour", "Ben Schwartz and Friends", "Matt Rife: Stay Golden World Tour",
                 "Nikki Glaser: The Stunning Tour", "Daniel Sloss: Bitter", "Wait Wait... Don't Tell Me!"}


def iso(value: str) -> str | None:
    value = value.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None  # TBD, Open run, blank


def min_price(cost: str) -> float | None:
    if cost.strip().lower() in ("free", "pay-what-you-can"):
        return 0
    nums = re.findall(r"\$\s*([0-9]+(?:\.[0-9]+)?)", cost)
    return float(nums[0]) if nums else None


def page_type(title: str, category: str) -> str:
    """The sheet's Category, except that Theater rows that are really concerts or comedy are split out."""
    if category != "Theater":
        return category
    if title in COMEDY_TITLES:
        return "Comedy"
    if any(re.search(p, title) for p in CONCERT):
        return "Concert"
    return "Theater"


def event_id(title: str, venue: str, begin: str) -> str:
    """The id worked out from a row's title, venue and start date, for rows with no ID in the sheet.
    Editing any of those three makes it a new event (the old one is withdrawn), which is why
    the sheet's ID column exists: fill it in and edits keep the same event."""
    return hashlib.sha1(f"{title}|{venue}|{begin}".encode()).hexdigest()[:6]


MAX_ID_LENGTH = 40  # events.source_id


def row_id(r: dict) -> tuple[str, bool]:
    """(id, came from the sheet's ID column). An ID too long to store falls back to the computed one."""
    given = r.get("id", "").strip()
    if given and len(given) <= MAX_ID_LENGTH:
        return given, True
    return event_id(r["event"], r["venue"], r["begin"]), False


def normalize(rows: list[dict]) -> list[dict]:
    by_venue: dict[str, Counter] = {}
    for r in rows:
        if r["category"]:
            by_venue.setdefault(r["venue"], Counter())[r["category"]] += 1
    events, seen = [], set()
    for r in rows:
        category = r["category"]
        if not category:  # blank Category: use what the venue's other rows say
            category = by_venue[r["venue"]].most_common(1)[0][0] if r["venue"] in by_venue else "Theater"
        eid, from_sheet = row_id(r)
        if eid in seen:  # the same event entered twice (or two rows given the same ID: the first wins)
            continue
        seen.add(eid)
        events.append({
            "id": eid, "title": r["event"], "venue": r["venue"],
            "begin": iso(r["begin"]), "end": iso(r["end"]),
            "openRun": r["end"].strip().lower() == "open run", "beginTbd": r["begin"].strip().upper() == "TBD",
            "hood": r["hood"], "area": AREA_LABELS[area_of(r["hood"])],
            "cost": r["cost"] or "TBD", "min": min_price(r["cost"]),
            "type": page_type(r["event"], category), "url": r["url"], "idFromSheet": from_sheet,
        })
    return events


def read_sheet(fetch=fetch_sheet_html) -> list[dict]:
    """Fetch and parse the sheet. Raises SheetUnavailable if it can't be read or understood."""
    return normalize(parse_sheet(fetch()))
