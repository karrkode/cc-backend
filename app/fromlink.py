"""CC-124: suggest an event by its link. The member pastes a link, the server reads the page and sends back a
draft for the Suggest form; nothing is saved but a line in the discovery log.

- Discovery's own reader: the robots.txt-aware fetch, the page's structured event data (JSON-LD) first, and
  Claude only when that finds no event. A Claude read is logged as a discovery check, so it counts toward the
  monthly budget; once the budget is used up, only structured data is tried.
- Only http(s) links to public hosts: no localhost, private networks or the cloud's internal addresses (SSRF).
  CC-189: the shared fetch makes sure of that for every request and redirect; check_public is the early answer.
- A few links a minute and a few dozen a day per member.
- CC-182: the page is fetched and read in a process of its own, stopped at DEADLINE, so no page can hold up the
  API's other requests. Only Claude's read (when there's no event data) runs in the request itself.
"""
import multiprocessing
import threading
from datetime import date
from functools import lru_cache
from time import monotonic
from urllib.parse import urlsplit

from .config import get_settings
from .discovery import fetch as web
from .discovery.budget import budget_state
from .discovery.fetch import PrivateAddress, check_public
from .discovery.jsonld import event_places, events_in, visible_title
from .discovery.pagetext import page_text
from .discovery.readers import FoundEvent, ReadFailed, read_page
from .discovery.rules import norm_title
from .categories import BY_SOURCE_KIND, CATEGORIES
from .models import DiscoveryCheck, DiscoverySource, Venue
from .ratelimit import SlidingWindowLimiter

PER_MINUTE, PER_DAY = 4, 30
minute_limiter, day_limiter = SlidingWindowLimiter(60), SlidingWindowLimiter(24 * 3600)
TIMEOUT = 10  # a member is waiting: discovery's 25 seconds is too long
# CC-182: the most fetching and reading one page may take in all, the most of it that's downloaded, and how many
# pages are read at once (a process each, on a small server)
DEADLINE, MAX_BYTES, READERS = 15, 3_000_000, threading.BoundedSemaphore(3)
RUN_DATES_MAX = 30  # as many dated shows as a fixed run takes (schemas.RUN_DATES_MAX)
NOTE = ("\n\nThis page was sent in by a member suggesting one event, so there's no usual venue: give each event's "
        "venue_name as the page states it. List the event the page is about first.")
WHY = {"blocked": "That site doesn't let us read its pages automatically.",
       "error": "We couldn't open that page.",
       "none": "We couldn't find an event on that page.",
       "busy": "We're reading a lot of links right now. Try again in a minute."}


class Unreadable(Exception):
    def __init__(self, why: str, detail: str = ""):
        super().__init__(detail or why)
        self.why = why


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


def _log(db, status: str, url: str, tin: int = 0, tout: int = 0, note: str = "", who: str = "a member's link") -> None:
    db.add(DiscoveryCheck(source_id=None, what="link", status=status, pages_read=1 if tin else 0,
                          input_tokens=tin, output_tokens=tout, note=f"{who}: {url}{' - ' + note if note else ''}"[:2000]))
    db.commit()


def _event_data(page) -> dict:
    events = events_in(page.text, page.url, "")
    got = {"url": page.url, "others": max(len(events) - 1, 0),
           "ai_opt_out": web.robots_for(page.url, TIMEOUT).ai_opt_out}  # already fetched: no request
    if events:
        found = _pick(events, page.url)
        got["place"] = event_places(page.text).get(norm_title(found.title), {})
        got["found"] = found.model_copy(update={"title": visible_title(found.title, page.text)}).model_dump()
    else:
        got["text"] = page_text(page.text, page.url)[0]
    return got


def fetch_and_read(url: str, read=_event_data) -> tuple[str, object]:
    """CC-182: everything the page itself decides: fetching it (robots.txt and redirects included) and reading it
    (its event data, or its text for Claude). ("ok", what was read) or (why, detail); run in its own process."""
    started = monotonic()
    try:
        page = web.fetch(url, ai=False, timeout=TIMEOUT, deadline=started + DEADLINE - 1, max_bytes=MAX_BYTES,
                         wait=False)
        return "ok", read(page)
    except PrivateAddress as e:
        return "private", str(e)
    except web.Blocked as e:
        return "blocked", str(e)
    except web.FetchError as e:
        return "error", str(e)
    except Exception as e:  # noqa: BLE001 - a page that breaks the reader is a page we couldn't read, not a server error
        return "error", f"couldn't read the page ({type(e).__name__})"


@lru_cache(maxsize=1)
def _workers():
    """Processes forked from a small server process that has the reader loaded, not from the API itself."""
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload(["app.fromlink", "app.venuelink"])
    return ctx


def _child(conn, work, url):
    try:
        conn.send(work(url))
    finally:
        conn.close()


def bounded_read(url: str, work=fetch_and_read) -> tuple[str, object]:
    """work(url) in a process of its own, stopped if it takes longer than DEADLINE."""
    if not READERS.acquire(blocking=False):
        return "busy", "too many links being read at once"
    try:
        ctx = _workers()
        recv, send = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_child, args=(send, work, url), daemon=True)
        proc.start()
        send.close()
        try:
            if recv.poll(DEADLINE):
                return recv.recv()
            return "error", f"reading the page took over {DEADLINE} seconds"
        except EOFError:
            return "error", "the page reader stopped"
        finally:
            recv.close()
            proc.kill()
            proc.join()
    finally:
        READERS.release()


def read_link(db, url: str, today: date) -> dict:
    """The draft for url, or raise PrivateAddress / Unreadable."""
    try:
        check_public(url)
    except web.FetchError as e:  # CC-223: no such host: a page we couldn't open, like one that fails in the read below
        why, got = "error", str(e)
    else:
        why, got = bounded_read(url)
    if why == "private":
        raise PrivateAddress(got)
    if why != "ok":
        _log(db, "error" if why == "busy" else why, url, note=got)
        raise Unreadable(why, got)
    if "found" in got:
        _log(db, "ok", url, note="read from the page's event data")
        return _draft(db, FoundEvent.model_validate(got["found"]), url, got["place"], "page", today, others=got["others"])
    state, _, _ = budget_state(db)
    client = claude_client() if state != "paused" else None
    if client is None or got["ai_opt_out"]:
        note = "no event data on the page" + (", and no Claude read" if client is None else ", and the site opts out of AI")
        _log(db, "none", url, note=note)
        raise Unreadable("none", note)
    if not got["text"].strip():
        _log(db, "none", url, note="the page has no text")
        raise Unreadable("none", "the page has no text")
    try:
        reading, tin, tout = read_page(client, "a page a member sent in", "an event page", got["url"], got["text"],
                                       today, NOTE)
    except ReadFailed as e:  # CC-198: the same, and its tokens count toward the budget
        _log(db, "error", url, e.tokens_in, e.tokens_out, note=f"Claude: {e}")
        raise Unreadable("error", f"Claude: {e}") from e
    except Exception as e:  # noqa: BLE001 - a failed read is a page we couldn't read, not a server error
        _log(db, "error", url, note=f"Claude: {type(e).__name__}: {e}")
        raise Unreadable("error", f"Claude: {type(e).__name__}: {e}") from e
    events = [e for e in reading.events if e.kind not in ("rental", "members_only", "permanent")]
    _log(db, "ok" if events else "none", url, tin, tout)
    if not events:
        raise Unreadable("none")
    return _draft(db, _pick(events, got["url"]), url, {}, "claude", today, others=len(events) - 1)


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
    named = db.query(Venue).filter(Venue.name.ilike(venue_name), Venue.pending.is_(False))  # CC-188: members don't see pending ones
    if source and not (venue_name and named.first()):
        # a venue's own site whose page names a stage ("Albert Theatre") or nothing: discovery knows the venue
        venue_name = source.venue.name if source.venue else source.venue_name or venue_name
        town = "" if source.venue or source.neighborhood else town
    begin, end = e.begin_date, e.end_date if place.get("end_given", True) else None
    if (end or begin or "9999") < today.isoformat():  # over, or a placeholder date (Goodman's pages give 1970-01-01)
        begin = end = None
    known = db.query(Venue).filter(Venue.name.ilike(venue_name), Venue.pending.is_(False)).all() if venue_name else []
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
