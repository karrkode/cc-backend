"""Filling in a venue's hours from what discovery reads (CC-78). Admins can edit both on the Venues
page, and an admin's version is never replaced (venue.*_by == "admin").

- Opening hours: what the venue's own page states (the reader's venue_hours).
- Usual show times: worked out from the dates and times found for the source's upcoming events. A
  start time counts for a weekday when it's listed on at least two different dates, so one special
  matinee doesn't become a regular one.
"""
from collections import defaultdict
from datetime import date

from sqlalchemy import select

from ..models import DiscoveredItem, DiscoverySource, Venue
from .rules import norm_title
from .store import _place

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PERFORMING = {"theater", "opera", "music", "comedy"}
SKIPPED = ("rejected", "skipped")


def _clock(t: str | None) -> str | None:
    """"19:30", "7:30", "19:30:00" -> "19:30"; anything else -> None."""
    try:
        h, m = (t or "").strip().split(":")[:2]
        h, m = int(h), int(m)
    except ValueError:
        return None
    return f"{h:02d}:{m:02d}" if 0 <= h < 24 and 0 <= m < 60 else None


def link_venue(db, source: DiscoverySource) -> bool:
    """A source added before its venue existed: link it once there's exactly one venue by that name
    (with or without "The"), for example after its first found event was approved."""
    if source.venue_id is not None or not source.venue_name:
        return False
    named = [v for v in db.scalars(select(Venue)) if _place(v.name) == _place(source.venue_name)]
    if len(named) != 1:
        return False
    source.venue_id, source.venue = named[0].id, named[0]
    return True


def apply_open_hours(source: DiscoverySource) -> bool:
    """Copy the opening hours the source's page stated onto its venue, unless an admin set them."""
    venue = source.venue
    if venue is None or venue.open_hours_by == "admin" or not source.venue_hours:
        return False
    hours = {}
    for day, (opens, closes) in source.venue_hours.items():
        if day in DAYS:
            o, c = _clock(opens), _clock(closes)
            hours[day] = [o, c] if o and c else None
    if not hours or hours == venue.open_hours:
        return False
    venue.open_hours, venue.open_hours_by = hours, "discovery"
    return True


def show_times_from(items: list[DiscoveredItem], today: date) -> dict:
    """{weekday: [start times]} for times listed on two or more upcoming dates."""
    dates_at = defaultdict(set)
    for item in items:
        for p in item.performances or []:
            t = _clock(p.get("time"))
            try:
                d = date.fromisoformat(p.get("date") or "")
            except ValueError:
                continue
            if t and d >= today:
                dates_at[(DAYS[d.weekday()], t)].add(d)
    times = defaultdict(list)
    for (day, t), dates in sorted(dates_at.items()):
        if len(dates) >= 2:
            times[day].append(t)
    return {day: times[day] for day in DAYS if day in times}


def apply_show_times(db, source: DiscoverySource, today: date) -> bool:
    """Work out the usual show times at the source's own venue from its found events, unless an admin set them."""
    venue = source.venue
    if venue is None or venue.show_times_by == "admin" or source.kind.value not in PERFORMING:
        return False
    here = {"", norm_title(source.venue_name), norm_title(venue.name)}
    items = [i for i in db.scalars(select(DiscoveredItem).where(DiscoveredItem.source_id == source.id,
                                                                DiscoveredItem.status.not_in(SKIPPED)))
             if norm_title(i.venue_name) in here]
    times = show_times_from(items, today)
    if not times or times == venue.show_times:
        return False
    venue.show_times, venue.show_times_by = times, "discovery"
    return True
