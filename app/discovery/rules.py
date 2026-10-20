"""What found events to leave out, as agreed with the product owner (CC-70):

- classes and workshops, private rentals, members-only events
- a museum's permanent galleries: marked so by the reader, or a museum exhibition with no dates
  that runs indefinitely
- generic offerings that repeat without a specific experience (family days, daily highlights tours):
  anything the reader marked recurring, or the same title on 3 or more dates within two weeks
Items left out are kept with a reason (status "skipped"), so the recap can count them.
Admins' own "skip similar" rules come later (CC-71).
"""
from collections import defaultdict
from datetime import date

from .readers import FoundEvent

LEAVE_OUT = {"class": "a class or workshop", "rental": "a private rental", "members_only": "members only",
             "recurring": "a recurring generic offering", "permanent": "a permanent gallery"}


def norm_title(title: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in title).split())


CLASS_WORDS = {"workshop", "workshops", "class", "classes", "course", "intensive", "bootcamp"}


def skip_reason(e: FoundEvent, same_title_dates: list[date], museum: bool = False) -> str:
    if e.kind in LEAVE_OUT:
        return LEAVE_OUT[e.kind]
    if museum and e.kind == "exhibition" and e.open_run and not e.end_date and not e.begin_date:
        return LEAVE_OUT["permanent"]
    if CLASS_WORDS & set(norm_title(e.title).split()):  # sources that don't say, such as the Art Institute's API
        return LEAVE_OUT["class"]
    if e.recurring:
        return LEAVE_OUT["recurring"]
    days = sorted(set(same_title_dates))
    if any((days[i + 2] - days[i]).days <= 14 for i in range(len(days) - 2)):
        return "repeats on 3 or more dates within two weeks"
    return ""


def _dates(e: FoundEvent) -> list[date]:
    out = []
    for d in [p.date for p in e.performances] + [e.begin_date if e.begin_date == e.end_date else None]:
        try:
            out.append(date.fromisoformat(d)) if d else None
        except ValueError:
            pass
    return out


def apply_rules(events: list[FoundEvent], kind: str) -> list[tuple[FoundEvent, str]]:
    """Each event with why it's left out ("" to keep). Repeat-detection is for museums' programs:
    productions and running shows legitimately play many nights."""
    by_title: dict[str, list[date]] = defaultdict(list)
    for e in events:
        by_title[norm_title(e.title)] += _dates(e)
    out = []
    for e in events:
        repeats = by_title[norm_title(e.title)] if kind == "museum" and e.kind in ("talk", "tour", "program", "other") else []
        out.append((e, skip_reason(e, repeats, museum=kind == "museum")))
    return out
