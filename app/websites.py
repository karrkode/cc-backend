"""CC-213: venue websites. Admin-only: checked and normalised here, and suggested for venues that have none.

Suggestions are worked out when asked for, never saved on their own: an admin confirms each one, and a
venue that has a website gets none (so a suggestion can't replace what an admin set)."""
import re
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DiscoverySource, Venue, VenueWebsiteDismissal

MAX_LEN = 500

# The official sites in the CC-205 report (section D) for venues already in the catalog, by venue name.
# A name that isn't in the catalog simply suggests nothing. Broadway In Chicago runs four of them.
CC205_WEBSITES = {
    "Broadway Playhouse": "https://www.broadwayinchicago.com/",
    "CIBC Theatre": "https://www.broadwayinchicago.com/",
    "Cadillac Palace Theatre": "https://www.broadwayinchicago.com/",
    "Nederlander Theatre": "https://www.broadwayinchicago.com/",
    "Chicago Shakespeare Theater": "https://www.chicagoshakes.com/",
    "Auditorium Theatre": "https://www.auditoriumtheatre.org/",
    "TimeLine Theatre": "https://timelinetheatre.com/",
    "Timeline Theater": "https://timelinetheatre.com/",
    "Athenaeum Center": "https://www.athenaeumcenter.org/",
    "Athenaeum Theatre": "https://www.athenaeumcenter.org/",
    "Theater Wit": "https://theaterwit.org/",
    "Hyde Park Art Center": "https://www.hydeparkart.org/",
    "Smart Museum of Art": "https://smartmuseum.uchicago.edu/",
    "Lookingglass Theatre": "https://lookingglasstheatre.org/",
    "Black Ensemble Theater": "https://www.blackensembletheater.org/",
}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).removeprefix("the ").strip()


_CC205 = {_key(name): url for name, url in CC205_WEBSITES.items()}


def normalize_website(raw: str) -> str:
    """An http(s) address, normalised: lower-case scheme and host, no default port or #fragment, "/" for an
    empty path. Raises ValueError for anything else. It's shown to admins as a link, so nothing but http(s)."""
    v = raw.strip()
    if not v:
        return ""
    # CC-176: untrusted text; characters a URL can't hold (quotes, angle brackets) are refused, not passed on
    if len(v) > MAX_LEN or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '<>"\'`\\{}|^' for c in v):
        raise ValueError("Enter a web address without spaces or quotes")
    try:
        parts = urlsplit(v)
        port = parts.port
    except ValueError:
        raise ValueError("That isn't a web address") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("A website must start with http:// or https://")
    host = (parts.hostname or "").rstrip(".")
    if not host or "." not in host or parts.username is not None or parts.password is not None:
        raise ValueError("That isn't a web address")
    scheme = parts.scheme.lower()
    netloc = host if port is None or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    url = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    if len(url) > MAX_LEN:
        raise ValueError("That web address is too long")
    return url


def _home(page: str) -> str:
    """The home page of the site a discovery source reads (its calendar is an events page, not the venue's site)."""
    try:
        return normalize_website(urlunsplit(urlsplit(page.strip())[:2] + ("/", "", "")))
    except ValueError:
        return ""


def suggestions(db: Session) -> list[dict]:
    """For each venue without a website: the home page of each discovery source linked to it, and its CC-205
    site, less the ones an admin dismissed. One row per venue and address."""
    venues = db.scalars(select(Venue).where(Venue.website == "")).all()
    if not venues:
        return []
    dismissed = set(db.execute(select(VenueWebsiteDismissal.venue_id, VenueWebsiteDismissal.url)).all())
    by_venue: dict[int, list[str]] = {}
    # Only sources that read the venue's own pages: one that reads an API (the Art Institute's) isn't its site.
    for venue_id, pages in db.execute(select(DiscoverySource.venue_id, DiscoverySource.pages)
                                      .where(DiscoverySource.venue_id.is_not(None), DiscoverySource.reader == "claude")
                                      .order_by(DiscoverySource.id)):
        if pages:
            by_venue.setdefault(venue_id, []).append(pages[0])
    out = []
    for v in sorted(venues, key=lambda v: (v.name.lower(), v.neighborhood.lower(), v.id)):
        found: dict[str, str] = {}
        for page in by_venue.get(v.id, []):
            if (url := _home(page)) and url not in found:
                found[url] = "discovery"
        if (url := _CC205.get(_key(v.name))) and url not in found:
            found[url] = "cc205"
        for url, source in found.items():
            if (v.id, url) not in dismissed:
                out.append({"venue_id": v.id, "venue_name": v.name, "neighborhood": v.neighborhood,
                            "url": url, "source": source})
    return out
