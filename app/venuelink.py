"""CC-222: add a venue by its website. An admin pastes the address, the server reads the page and sends back what it
says for the Add a venue form; nothing is saved but a line in the discovery log.

- The Suggest form's reader (fromlink): the robots.txt-aware fetch in a process of its own, stopped at its deadline,
  with the same check of every address (CC-189).
- The page's schema.org data first, which is free. Claude reads the page only when something the form requires is
  still missing, once per website read, and it counts toward the monthly budget. With the budget used up or an AI
  opt-out, only the free read runs. Claude never replaces what the structured data gave.
- A page that can't be read is a `problem` rather than an error, so the form can still say which venue has this website.
"""
from sqlalchemy import select

from . import fromlink
from .addresses import found_address, found_town, normalize_address
from .discovery import fetch as web
from .discovery.budget import budget_state
from .discovery.fetch import PrivateAddress, check_public
from .discovery.jsonld import venue_in
from .discovery.pagetext import page_text
from .discovery.readers import read_venue_page
from .discovery.venuesources import _parts
from .models import Venue

WHO = "an admin's venue website"
FIELDS = ("name", "street", "city", "state", "zip_code")


def _venue_data(page) -> dict:
    got = {"url": page.url, "found": venue_in(page.text),
           "ai_opt_out": web.robots_for(page.url, fromlink.TIMEOUT).ai_opt_out}  # already fetched: no request
    if not _complete(got["found"]):
        got["text"] = page_text(page.text, page.url, footers=True)[0]
    return got


def fetch_venue(url: str) -> tuple[str, object]:
    """fromlink.bounded_read's work: a name of its own, since it's sent to another process."""
    return fromlink.fetch_and_read(url, _venue_data)


def _complete(found: dict) -> bool:
    return all(found[f] for f in ("name", "street", "city", "zip_code"))


def _checked(found: dict) -> dict:
    """found, less what a page can't be trusted on: a street that isn't one ("See website"), or a city or ZIP that
    doesn't check out. A filled-in field is one the admin may not look at twice."""
    whole = found_address(found["street"], found["city"], found["zip_code"])
    street, city, zip_code = whole or ("", *found_town(found["city"], found["zip_code"]))
    return {**found, "name": " ".join(found["name"].split())[:200], "street": street, "city": city, "zip_code": zip_code}


def _neighborhood(found: dict, stated: str = "") -> str:
    """A suburb's town as the form's hint asks for it ("Oak Park, IL"), or the neighborhood the page names. Chicago
    alone says nothing, and with no state a town is left for the admin: "Milwaukee" isn't "Milwaukee, IL"."""
    city, state = found["city"], found["state"].upper()
    if city and city.lower() != "chicago" and len(state) == 2:
        return f"{city}, {state}"[:120]
    try:
        return normalize_address(stated)[:120]
    except ValueError:
        return ""


def same_website(db, url: str) -> list[dict]:
    """The venues that already have this website, http or https and with or without www."""
    site = _parts(url)
    venues = db.scalars(select(Venue).where(Venue.website != "").order_by(Venue.name, Venue.id))
    return [{"name": v.name, "neighborhood": v.neighborhood} for v in venues if _parts(v.website) == site]


def _ask_claude(db, url: str, got: dict) -> dict:
    """What Claude adds to the structured data, or nothing when it may not read this page. Logs either way."""
    found = got["found"]
    state, _, _ = budget_state(db)
    client = fromlink.claude_client() if state != "paused" else None
    if client is None or got["ai_opt_out"] or not got["text"].strip():
        why = "no Claude read" if client is None else "the site opts out of AI" if got["ai_opt_out"] else "the page has no text"
        fromlink._log(db, "none", url, note=f"not all there in the page's data, and {why}", who=WHO)
        return {}
    try:
        reading, tin, tout = read_venue_page(client, got["url"], got["text"])
    except Exception as e:  # noqa: BLE001 - a failed read leaves what the page's data gave
        fromlink._log(db, "error", url, note=f"Claude: {type(e).__name__}: {e}", who=WHO)
        return {}
    added = reading.model_dump()
    fromlink._log(db, "ok" if any(added.values()) else "none", url, tin, tout, who=WHO)
    return {**{f: found[f] or added[f] for f in FIELDS}, "neighborhood": added["neighborhood"]}


def read_site(db, url: str) -> dict:
    """The draft for the form. url has been through normalize_website."""
    draft = {"website": url, "same_website": same_website(db, url), "missing": ["name", "address", "city", "zip_code"]}
    try:
        check_public(url)
        why, got = fromlink.bounded_read(url, fetch_venue)
    except PrivateAddress as e:
        why, got = "private", str(e)
    except web.FetchError as e:  # no such host
        why, got = "error", str(e)
    if why == "private":
        return {**draft, "problem": f"We can't read that link: {got}."}
    if why != "ok":
        fromlink._log(db, "error" if why == "busy" else why, url, note=got, who=WHO)
        return {**draft, "problem": fromlink.WHY[why]}
    found, stated = _checked(got["found"]), ""
    if _complete(found):
        fromlink._log(db, "ok", url, note="read from the page's structured data", who=WHO)
    else:
        added = _ask_claude(db, url, {**got, "found": found})
        stated = added.pop("neighborhood", "")
        found = _checked(added) if added else found
    fields = {"name": found["name"], "address": found["street"], "city": found["city"], "zip_code": found["zip_code"]}
    return {**draft, **fields, "neighborhood": _neighborhood(found, stated), "missing": [f for f, v in fields.items() if not v]}
