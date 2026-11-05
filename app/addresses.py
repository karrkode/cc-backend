"""CC-218: venue street addresses. Public, unlike the website: an address is what people need to get there.

An address is three fields, kept apart so the event data can give each one: `address` (the street), `city` and
`zip_code`. A venue has all three or none; the state comes from the venue, as it does for the event data. Only an
admin sets them, by typing them or confirming a suggestion. Discovery only ever adds suggestions
(`venue_address_suggestions`), so it can't change what a venue shows."""
import re
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Area, Venue, VenueAddressSuggestion

MAX_LEN = 200
ZIP = re.compile(r"\d{5}(-\d{4})?")


def normalize_address(raw: str) -> str:
    """One line with single spaces; "" clears it. Raises ValueError for what can't be an address."""
    v = " ".join(raw.split())
    # CC-176: untrusted text; it's shown publicly, so markup and web addresses are refused, not passed on
    if len(v) > MAX_LEN or any(ord(c) < 32 or ord(c) == 127 or c in "<>{}\\" for c in v) or "://" in v:
        raise ValueError("Enter a street address, like 78 E Washington St")
    return v


def normalize_city(raw: str) -> str:
    v = normalize_address(raw)
    if len(v) > 120 or re.search(r"\d", v):
        raise ValueError("Enter a city, like Chicago")
    return v


def normalize_zip(raw: str) -> str:
    v = raw.strip()
    if v and not ZIP.fullmatch(v):
        raise ValueError("Enter a 5-digit ZIP code")
    return v


def found_address(street: str, city: str, zip_code: str) -> tuple[str, str, str] | None:
    """What a page's event data gives, if it's worth suggesting: the street needs a number and a word, since a page
    can say anything ("See website", "TBA"). A city or ZIP that doesn't check out is left for the admin to fill in."""
    try:
        v = normalize_address(street)
    except ValueError:
        return None
    if not (re.search(r"\d", v) and re.search(r"[A-Za-z]{2}", v)):
        return None
    try:
        city = normalize_city(city)
    except ValueError:
        city = ""
    try:
        zip_code = normalize_zip(zip_code)
    except ValueError:
        zip_code = ""
    return v, city, zip_code


def locality(area, neighborhood: str) -> tuple[str, str]:
    """(town, state): "Oak Park, IL" -> ("Oak Park", "IL"), "Racine, WI" -> ("Racine", "WI"); the city is Chicago."""
    where = neighborhood.strip()
    if area in (Area.city, Area.city.value) or not where:
        return "Chicago", "IL"
    m = re.fullmatch(r"(.+?),\s*([A-Z]{2})", where)
    return (m.group(1), m.group(2)) if m else (where, "IL")


def full_address(address: str, city: str, zip_code: str, area, neighborhood: str) -> str:
    """"78 E Washington St, Chicago, IL 60602". "" with no address."""
    if not address:
        return ""
    town, state = locality(area, neighborhood)
    return f"{address}, {city or town}, {state} {zip_code}".strip()


def map_url(full: str) -> str:
    """A plain link to a maps search: nothing is embedded, and nothing is sent until someone follows it."""
    return f"https://www.google.com/maps/search/?api=1&query={quote(full, safe='')}" if full else ""


def suggestions(db: Session) -> list[dict]:
    """What discovery has seen for venues with no address, less the ones an admin dismissed."""
    rows = db.execute(select(VenueAddressSuggestion, Venue).join(Venue)
                      .where(Venue.address == "", VenueAddressSuggestion.dismissed_at.is_(None))).all()
    rows.sort(key=lambda r: (r[1].name.lower(), r[1].neighborhood.lower(), r[1].id, r[0].address))
    return [{"venue_id": v.id, "venue_name": v.name, "neighborhood": v.neighborhood, "address": s.address,
             "city": s.city or locality(v.area, v.neighborhood)[0], "zip_code": s.zip_code, "source": s.source}
            for s, v in rows]
