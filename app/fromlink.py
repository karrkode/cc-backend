"""CC-124: suggest an event by its link. The member pastes a link, the server reads the page and sends back a
draft for the Suggest form; nothing is saved but a line in the discovery log.

- Discovery's own reader: the robots.txt-aware fetch, the page's structured event data (JSON-LD) first, and
  Claude only when that finds no event. A Claude read is logged as a discovery check, so it counts toward the
  monthly budget; once the budget is used up, only structured data is tried.
- Only http(s) links to public hosts, checked again on every redirect: no localhost, private networks or the
  cloud's internal addresses (SSRF).
- A few links a minute and a few dozen a day per member.
"""
import ipaddress
import socket
from datetime import date
from functools import lru_cache
from time import monotonic
from urllib.parse import urlsplit

from .config import get_settings
from .discovery import fetch as web
from .discovery.budget import budget_state
from .discovery.jsonld import event_places, events_in
from .discovery.pagetext import page_text
from .discovery.readers import FoundEvent, read_page
from .discovery.rules import norm_title
from .categories import BY_SOURCE_KIND, CATEGORIES
from .models import DiscoveryCheck, DiscoverySource, Venue
from .ratelimit import SlidingWindowLimiter

PER_MINUTE, PER_DAY = 4, 30
minute_limiter, day_limiter = SlidingWindowLimiter(60), SlidingWindowLimiter(24 * 3600)
TIMEOUT = 10  # a member is waiting: discovery's 25 seconds is too long
RUN_DATES_MAX = 30  # as many dated shows as a fixed run takes (schemas.RUN_DATES_MAX)
NOTE = ("\n\nThis page was sent in by a member suggesting one event, so there's no usual venue: give each event's "
        "venue_name as the page states it. List the event the page is about first.")
WHY = {"blocked": "That site doesn't let us read its pages automatically.",
       "error": "We couldn't open that page.",
       "none": "We couldn't find an event on that page."}


class PrivateAddress(web.Blocked):
    """The link (or a redirect) points somewhere that isn't a public web host."""


class Unreadable(Exception):
    def __init__(self, why: str, detail: str = ""):
        super().__init__(detail or why)
        self.why = why


def _addresses(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def check_public(url: str) -> None:
    """Raise PrivateAddress unless url is http(s) on the usual port, at a host whose every address is public."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise PrivateAddress("only http and https links to public websites can be read")
    try:
        port = parts.port
    except ValueError as e:
        raise PrivateAddress("that link has a port we can't read") from e
    if port not in (None, 80, 443):
        raise PrivateAddress("only links on the usual web ports can be read")
    try:
        found = _addresses(parts.hostname, port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError) as e:
        raise web.FetchError(f"there's no website at {parts.hostname}") from e
    for a in found:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        ip = ip.ipv4_mapped or ip if ip.version == 6 else ip
        if not ip.is_global:  # loopback, private, link-local (169.254.169.254), carrier-grade NAT, reserved...
            raise PrivateAddress("that link points to a private or local address")


@lru_cache(maxsize=1)
def _ssm_key(_: int) -> str:
    import boto3  # production only; the container's short-lived credentials can read this one parameter
    s = get_settings()
    got = boto3.session.Session().client("ssm", region_name=s.ssm_region).get_parameter(
        Name=s.anthropic_key_param, WithDecryption=True)
    return got["Parameter"]["Value"]


def claude_client():
    """A Claude client, or None when there's no key (locally, unless ANTHROPIC_API_KEY is set)."""
    s = get_settings()
    key = s.anthropic_api_key
    if not key and s.env == "prod":
        try:
            key = _ssm_key(int(monotonic() // 600))  # read again every 10 minutes, so a new key is picked up
        except Exception:  # noqa: BLE001 - no key means structured data only, never a failed request
            key = ""
    if not key or key == "unset":
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=1)


def _pick(events: list[FoundEvent], url: str) -> FoundEvent | None:
    """The event the page is about: the one linking to this page, else the first."""
    def bare(u):
        return (u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    return next((e for e in events if bare(e.url) == bare(url)), events[0] if events else None)


def _log(db, status: str, url: str, tin: int = 0, tout: int = 0, note: str = "") -> None:
    db.add(DiscoveryCheck(source_id=None, what="link", status=status, pages_read=1 if tin else 0,
                          input_tokens=tin, output_tokens=tout, note=f"a member's link: {url}{' - ' + note if note else ''}"[:2000]))
    db.commit()


def read_link(db, url: str, today: date) -> dict:
    """The draft for url, or raise PrivateAddress / Unreadable."""
    check_public(url)
    try:
        page = web.fetch(url, ai=False, check=check_public, timeout=TIMEOUT)
        events, how = events_in(page.text, page.url, ""), "page"
        if not events:
            state, _, _ = budget_state(db)
            client = claude_client() if state != "paused" else None
            if client is None or web.robots_for(page.url, check_public, TIMEOUT).ai_opt_out:
                raise Unreadable("none", "no event data on the page" + (", and no Claude read" if client is None else
                                                                         ", and the site opts out of AI"))
            text, _ = page_text(page.text, page.url)
            if not text.strip():
                raise Unreadable("none", "the page has no text")
            try:
                reading, tin, tout = read_page(client, "a page a member sent in", "an event page", page.url, text, today, NOTE)
            except Exception as e:  # noqa: BLE001 - a failed read is a page we couldn't read, not a server error
                raise Unreadable("error", f"Claude: {type(e).__name__}: {e}") from e
            events = [e for e in reading.events if e.kind not in ("rental", "members_only", "permanent")]
            _log(db, "ok" if events else "none", url, tin, tout)
            how = "claude"
            if not events:
                raise Unreadable("none")
        else:
            _log(db, "ok", url, note="read from the page's event data")
    except PrivateAddress:
        raise
    except web.Blocked as e:
        _log(db, "blocked", url, note=str(e))
        raise Unreadable("blocked", str(e)) from e
    except web.FetchError as e:
        _log(db, "error", url, note=str(e))
        raise Unreadable("error", str(e)) from e
    except Unreadable as e:
        if e.why != "none" or how == "page":
            _log(db, e.why if e.why != "none" else "none", url, note=str(e))
        raise
    found = _pick(events, page.url)
    place = event_places(page.text).get(norm_title(found.title), {}) if how == "page" else {}
    return _draft(db, found, url, place, how, today, others=len(events) - 1)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _source_for(db, url: str) -> DiscoverySource | None:
    """The discovery source whose pages are on this link's site, if there's exactly one: a venue's own site."""
    host = _host(url)
    found = [s for s in db.query(DiscoverySource).all() if any(_host(p) == host for p in s.pages or [])]
    return found[0] if len(found) == 1 else None


def _draft(db, e: FoundEvent, url: str, place: dict, how: str, today: date, others: int) -> dict:
    """place: what the page's event data says beyond the event (jsonld.event_places), for a page read without Claude."""
    venue_name, town, ld_category = place.get("venue", ""), place.get("town", ""), place.get("category")
    venue_name = (e.venue_name or venue_name).strip()
    source = _source_for(db, url)
    if source and not (venue_name and db.query(Venue).filter(Venue.name.ilike(venue_name)).first()):
        # a venue's own site whose page names a stage ("Albert Theatre") or nothing: discovery knows the venue
        venue_name = source.venue.name if source.venue else source.venue_name or venue_name
        town = "" if source.venue or source.neighborhood else town
    begin, end = e.begin_date, e.end_date if place.get("end_given", True) else None
    if (end or begin or "9999") < today.isoformat():  # over, or a placeholder date (Goodman's pages give 1970-01-01)
        begin = end = None
    known = [v for v in db.query(Venue).filter(Venue.name.ilike(venue_name)).all()] if venue_name else []
    hoods = {v.neighborhood for v in known if v.neighborhood.strip()}
    if len(hoods) == 1:
        hood = hoods.pop()
    elif source and not known and source.neighborhood and venue_name == source.venue_name:
        hood = source.neighborhood
    elif town and town.lower() != "chicago" and not known:
        hood = f"{town}, IL"  # a suburb: the form's own hint says to add ", IL"
    else:
        hood = ""
    defaults = {v.category for v in known if v.category}
    category = e.category or ld_category or (defaults.pop() if len(defaults) == 1 else None) or \
        (BY_SOURCE_KIND[source.kind.value][0] if source and source.kind.value in BY_SOURCE_KIND else None)
    if category not in CATEGORIES:  # CC-133: a comedy club's page, say; the member picks one of the three
        category = None
    shows = sorted({p.date: p for p in e.performances if p.time and p.date >= today.isoformat()}.values(), key=lambda p: p.date)
    run_dates = [{"date": p.date, "time": p.time} for p in shows] if 0 < len(shows) <= RUN_DATES_MAX else []
    weekly = {d.weekday: d.times[:6] for d in e.show_days if d.times} if not run_dates else {}
    weeks = {d.weekday: d.weeks for d in e.show_days if d.times and d.weeks} if weekly else {}
    draft = {"url": url, "title": e.title.strip()[:300],
             "category": category, "venue": venue_name[:200], "neighborhood": hood[:120],
             "begin_date": begin, "end_date": end, "open_run": e.open_run,
             "cost_text": (e.cost_text or "")[:120], "min_price": e.min_price, "run_dates": run_dates,
             "weekly": weekly, "weeks": weeks, "read_by": how, "other_events": others}
    if run_dates:  # a fixed run's dates are its opening and closing
        draft["begin_date"], draft["end_date"] = run_dates[0]["date"], run_dates[-1]["date"]
    draft["missing"] = [f for f, has in (("title", draft["title"]), ("category", category), ("venue", venue_name),
                                         ("neighborhood", hood), ("dates", draft["begin_date"] or run_dates),
                                         ("price", draft["cost_text"])) if not has]
    return draft
