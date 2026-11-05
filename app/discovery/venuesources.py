"""CC-221: a source for every venue with a website, and the numbers behind the Event sources grid.

- A venue's website (venues.website) and a source's sourcing URL (discovery_sources.pages) are different things: a
  website is usually a home page, which rarely lists events. A new source starts on the home page and, just before its
  first read, moves to the calendar link found there, if any. That step reads no more than one free page.
- All the sources are added at once, but only FIRST_READS_PER_DAY are due each day, so the review queue grows steadily.
- A website shared by several venues (Broadway In Chicago runs four) is one source, and its block counts them all.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import re

from sqlalchemy import func, or_, select

from ..areas import is_out_of_town
from ..models import DiscoveredItem, DiscoverySource, Event, EventStatus, SourceKind, Venue
from . import fetch as web
from .store import _place, on_source_site

FIRST_READS_PER_DAY = 10
SIZE_STEPS = (3, 10, 25)  # a block's size goes up a step at each of these approved events
KINDS = {"Music": SourceKind.music, "Visual Art": SourceKind.museum}  # anything else reads as theater
# Best first. Words are compared after _words(), which drops the apostrophe in "what's on".
LINK_WORDS = ("calendar", "events", "shows", "whats on", "upcoming", "performances", "exhibitions", "productions", "season")
NOT_LISTINGS = ("private", "past", "rental", "rent", "wedding", "host", "archive", "gift", "donate", "subscri")
MAX_LINK_DEPTH = 2  # /visit/calendar yes; /events/2026/a-single-show is one event, not the list


def _parts(url: str) -> tuple[str, str]:
    """A web address's host, without www., and its path: what makes two addresses the same website."""
    try:
        p = urlsplit(url or "")
        return (p.hostname or "").lower().removeprefix("www."), p.path.rstrip("/")
    except ValueError:
        return "", ""


def covers(source: DiscoverySource, venue: Venue) -> bool:
    """Whether the source already reads this venue: it's linked, has its name, or reads its website."""
    if source.venue_id == venue.id or _place(venue.name) in {_place(source.venue_name), _place(source.venue.name if source.venue else "")}:
        return True
    host, path = _parts(venue.website)
    if not host:
        return False
    return (host, path) == _parts(source.added_from) or \
        any(h == host and (not path or p == path) for h, p in map(_parts, source.pages or []))


def _new_source(venue: Venue, day: date) -> DiscoverySource:
    kind = SourceKind.opera if venue.opera else KINDS.get(venue.category, SourceKind.theater)
    return DiscoverySource(name=venue.name, kind=kind, pages=[venue.website], reader="claude", venue=venue, venue_name=venue.name,
                           neighborhood=venue.neighborhood, next_check_on=day, added_from=venue.website, find_link=True)


def add_missing(db, today: date) -> int:
    """Add a source for each website no source covers, and say how many. Runs with every discovery run, so a venue
    given a website later joins by itself."""
    sources = list(db.scalars(select(DiscoverySource)))
    venues = db.scalars(select(Venue).where(Venue.website != "", Venue.pending.is_(False), Venue.hidden.is_(False))
                        .order_by(func.lower(Venue.name), Venue.id))
    sites: dict[tuple[str, str], list[Venue]] = {}
    for venue in venues:
        if not is_out_of_town(venue):
            sites.setdefault(_parts(venue.website), []).append(venue)
    waiting = Counter(max(s.next_check_on or today, today) for s in sources
                      if s.active and not s.manual and not s.last_status and not s.last_checked_at)
    names, added, day = {s.name for s in sources}, 0, today
    for group in sites.values():
        if any(v.no_source for v in group) or group[0].name in names or any(covers(s, v) for s in sources for v in group):
            continue
        while waiting[day] >= FIRST_READS_PER_DAY:
            day += timedelta(days=1)
        source = _new_source(group[0], day)
        db.add(source)
        sources.append(source); names.add(source.name); waiting[day] += 1; added += 1
    return added


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower().replace("'", "").replace("’", "")).split())


class _Links(HTMLParser):
    """Every link on a page with its text. pagetext drops navigation, which is where a calendar link usually is."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.href, self.text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href, self.text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((self.href, _words(" ".join(self.text))))
            self.href = None


def _score(url: str, text: str) -> int:
    """Higher is better, and 0 isn't a listing: the link's whole text, then a word in it, then a word in its path."""
    path = urlsplit(url).path.lower()
    segments = [s for s in path.split("/") if s]
    if len(segments) > MAX_LINK_DEPTH or path.endswith((".pdf", ".ics")) or any(w in text or w in path for w in NOT_LISTINGS):
        return 0
    n = len(LINK_WORDS)
    for rank, word in enumerate(LINK_WORDS):
        if text == word:
            return 3 * n - rank
    for rank, word in enumerate(LINK_WORDS):
        if word in text:
            return 2 * n - rank
    for rank, word in enumerate(LINK_WORDS):
        if word.replace(" ", "-") in path:
            return n - rank
    return 0


def calendar_link(html: str, base: str, source: DiscoverySource) -> str | None:
    """The best link on a home page to its events, on the source's own site. The first on the page wins a tie."""
    parser = _Links()
    parser.feed(html)
    best, best_score = None, 0
    for href, text in parser.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base, href).split("#")[0]
        if _parts(url) == _parts(base) or not on_source_site(url, source):
            continue
        score = _score(url, text)
        if score > best_score:
            best, best_score = url, score
    return best


def use_calendar_link(source: DiscoverySource) -> None:
    """Move a new source from its home page to the calendar link there. Blocked and FetchError reach check_source,
    which reports them as for any page, and find_link stays set so the next check tries again."""
    page = web.fetch(source.pages[0])
    link = calendar_link(page.text, page.url, source)
    if link:
        source.pages = [link]
    source.find_link = False


def state_of(source: DiscoverySource, found: int) -> str:
    """The block's colour. Check by hand beats an error: an admin has already dealt with that one."""
    if not source.active:
        return "off"
    if source.manual:
        return "manual"
    if source.last_status in ("blocked", "error"):
        return "error"
    if not source.last_status:
        return "new"
    return "ok" if found else "nothing"


def size_of(events: int) -> int:
    return 1 + sum(events >= step for step in SIZE_STEPS)


@dataclass
class GridNumbers:
    events_year: int = 0
    size: int = 1
    found: int = 0
    approved: int = 0


def grid_numbers(db, sources: list[DiscoverySource], today: date) -> dict[int, GridNumbers]:
    """Per source: approved events at its venues from 12 months ago onward, which sets the block's size, and what it
    has found. An open run still counts whenever it began, and an event with no dates doesn't count."""
    since = today - timedelta(days=365)
    per_venue = dict(db.execute(select(Event.venue_id, func.count())
                                .where(Event.status == EventStatus.approved,
                                       or_(func.coalesce(Event.end_date, Event.begin_date) >= since,
                                           Event.open_run.is_(True) & Event.begin_date.is_not(None)))
                                .group_by(Event.venue_id)).all())
    found = dict(db.execute(select(DiscoveredItem.source_id, func.count()).group_by(DiscoveredItem.source_id)).all())
    approved = dict(db.execute(select(DiscoveredItem.source_id, func.count()).join(Event, DiscoveredItem.event_id == Event.id)
                               .where(DiscoveredItem.status == "approved", Event.status == EventStatus.approved)
                               .group_by(DiscoveredItem.source_id)).all())
    venues = db.scalars(select(Venue)).all()
    numbers = {}
    for source in sources:
        events = sum(per_venue.get(v.id, 0) for v in venues if covers(source, v))
        numbers[source.id] = GridNumbers(events, size_of(events), found.get(source.id, 0), approved.get(source.id, 0))
    return numbers
