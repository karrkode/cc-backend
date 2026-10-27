"""CC-125: the five event categories, and opera, the one event that's listed under two.

An event has one category from CATEGORIES. Opera is a flag an admin sets on a Music event: it's also listed under
Theater in the catalog filter, and labelled "Opera" on its card and page. Stats count it once, under Music.
A venue carries a default category (with the opera flag) that new events at it start with; changing it never
changes the venue's existing events.
"""
from sqlalchemy import func, or_, select

from .models import Event, Venue

CATEGORIES = ("Theater", "Comedy", "Music", "Visual Art", "Educational")  # the order the site shows them in
OPERA_IS = "Music"  # an opera's own category
OPERA_ALSO = "Theater"  # and the one it's also listed under
OLD_NAMES = {"Concert": "Music"}  # the old free-text types that map onto the five

# Discovery's source kinds: (category, opera)
BY_SOURCE_KIND = {"theater": ("Theater", False), "music": ("Music", False), "comedy": ("Comedy", False),
                  "museum": ("Visual Art", False), "opera": (OPERA_IS, True)}


def label(category: str, opera: bool) -> str:
    """What a card or event page calls it."""
    return "Opera" if opera else category


def category_condition(categories: list[str]):
    """Events in any of these categories; an opera also matches Theater (one row, so it's listed once)."""
    cond = Event.category.in_(categories)
    return or_(cond, Event.opera.is_(True)) if OPERA_ALSO in categories else cond


def fill_venue_defaults(db) -> None:
    """Give each venue without a default its events' most common category (ties: alphabetical). The 0033
    migration did this once in SQL; seeding uses it for venues it adds."""
    for venue in db.scalars(select(Venue).where(Venue.category.is_(None))):
        top = db.execute(select(Event.category, func.count(Event.id)).where(Event.venue_id == venue.id,
                                                                            Event.category.in_(CATEGORIES))
                         .group_by(Event.category).order_by(func.count(Event.id).desc(), Event.category)).first()
        if top:
            venue.category = top[0]
