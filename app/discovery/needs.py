"""What a found event still needs before it can go in the catalog (CC-88, agreed with the product owner).

- Performances (Theater, Comedy, Concert; musicals count as theater): a date, and start times: show
  days with times, dated performances with times, or the venue's usual show times. A show with an open
  run and its show days (a weekly open mic) is on now, so it doesn't need a date.
- Everything else (exhibitions, talks, tours): an opening or closing date, unless it's an open run.
- A museum's permanent galleries need nothing.
An event that's missing something has its own page read again (run.check_missing): on the next day's
run after it's found, then once a week for 4 weeks. It waits in review, marked, until an admin adds
what's missing in the approve form or rejects it.
"""
from datetime import date

from ..models import DiscoveredItem, Venue
from .rules import PERFORMING_CATEGORIES

LOOK_EVERY_DAYS = 7
LOOK_FOR_DAYS = 28


def needs(item: DiscoveredItem, venue: Venue | None) -> str:
    """"date", "show_days" or "" (nothing missing)."""
    if item.kind == "permanent":
        return ""
    dated = bool(item.begin_date or item.end_date or item.performances)
    if item.category not in PERFORMING_CATEGORIES:
        return "" if dated or item.open_run else "date"
    timed = bool(item.weekly) or any(p.get("time") for p in item.performances or []) or bool(venue and venue.show_times)
    if not dated and not (item.open_run and timed):
        return "date"
    return "" if timed else "show_days"


def look_again(item: DiscoveredItem, seen: date, today: date) -> bool:
    """Whether today is a day to read the item's own page for what it's missing (seen: the day it was found)."""
    if today <= seen or (today - seen).days > LOOK_FOR_DAYS:
        return False
    looked = [date.fromisoformat(d) for d in item.looked_on or []]
    return not looked or (today - max(looked)).days >= LOOK_EVERY_DAYS
