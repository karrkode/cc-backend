"""Each sourced event's picture, from the site it was sourced from (CC-246, as agreed with the product owner).

- Which events: catalog events approved from discovery (found_by_id) that have no photo and a page of their own.
  Events from before CC-246 were looked at by CC-250's one-off search, so they're never looked at again.
- Where from: the event's own page, its preview picture (og:image, then twitter:image, then the page's schema.org
  event data). Never one an admin picks, and never another site's: the page is the event's link, and the picture
  is whatever that page names.
- Safely: the page and the picture are fetched like any page (app/discovery/fetch.py: public hosts only, every
  redirect checked, robots.txt, the gap between requests to one site, a size cap). Only JPEG, PNG, WebP, GIF and
  AVIF are taken, and only when the bytes agree with the type. Pillow then re-makes it as our own WebP, upright,
  without metadata and within a pixel cap (app/event_images.py), so what we serve is never the file we fetched.
- Copied, never hotlinked: it's stored like CC-250's photos and served from chiculturecrew.com/event-images/.
  image_source_url keeps the page it came from, which the event page credits.
- A failure never holds anything up: the event stays live with its water. A page or picture that can't be fetched
  this time is tried again the next day, at most MAX_TRIES times; a page with no usable picture isn't tried again.
- CC-310: a found item gets its candidate picture the same way, before anyone approves it (add_item_picture): from its
  own page, else from up to two tickets or calendar links on it (no model: the links are the page's own, picked by their
  words). The admin sees the picture, or why there isn't one, and their upload or removal is never replaced.
- A tiny picture (an icon), or one another event already has (a site's logo or default share image), isn't used.
"""
import io
import re
import urllib.error
from datetime import UTC, datetime, timedelta
from html import unescape
from urllib.parse import urljoin, urlsplit

from sqlalchemy import and_, or_, select

from ..clock import utcnow
from .. import event_images
from ..models import DiscoveredItem, DiscoverySource, Event, EventStatus
from . import fetch as web
from . import htmlpics
from .jsonld import _blocks, _is_event, _objects, _tags
from .readers import aic_image_url
from .store import on_source_site
from .tickets import links_of

MAX_TRIES = 3
RETRY_AFTER = timedelta(hours=20)  # about a day: the hourly run looks again tomorrow, not next hour
PER_RUN = 10
FINDER_VERSION = 2  # CC-321: 2 = also the listing card and the page's main picture. CC-320: bump when the finder learns to fix a kind of miss; items looked at by an older one get one more look
MAX_BYTES = 10_000_000  # a share image is usually well under 1 MB
BLANK_STDDEV = 2.0  # CC-321: below this spread of brightness in every channel, a picture is one colour
MIN_SIDE = 200  # smaller than this on its short side is an icon, not an event's picture
ACCEPT = "image/webp,image/avif,image/jpeg,image/png,image/gif;q=0.8"
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}
META_OPEN = re.compile(r"<(?:meta|link)\b", re.I)
ATTR = re.compile(r'([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))')
# the preview picture's names, best first
NAMES = ("og:image:secure_url", "og:image:url", "og:image", "twitter:image", "twitter:image:src")


class NoPicture(Exception):
    """The page has no picture we can use. Not worth trying again."""


def _attrs(tag: str) -> dict[str, str]:
    return {m[1].lower(): unescape(m[3] or m[4] or m[5] or "").strip() for m in ATTR.finditer(tag)}


def _web_address(url: str, page_url: str) -> str | None:
    try:
        full = urljoin(page_url, url.strip())
        parts = urlsplit(full)
    except ValueError:
        return None
    return full if parts.scheme in ("http", "https") and parts.hostname else None


def preview_images(page_html: str, page_url: str) -> list[str]:
    """The page's preview pictures, best first, as full http(s) addresses."""
    named: dict[str, str] = {}
    for tag, _ in _tags(page_html or "", META_OPEN):
        a = _attrs(tag)
        name = (a.get("property") or a.get("name") or "").lower()
        if tag[1:5].lower() == "meta" and name in NAMES and a.get("content") and name not in named:
            named[name] = a["content"]
        elif tag[1:5].lower() == "link" and a.get("rel", "").lower() == "image_src" and a.get("href"):
            named.setdefault("image_src", a["href"])
    found = [named[n] for n in (*NAMES, "image_src") if n in named]
    for data in _blocks(page_html or ""):
        for obj in _objects(data):
            if _is_event(obj):
                found += _schema_images(obj.get("image"))
    out = []
    for u in found:
        full = _web_address(u, page_url)
        if full and full not in out:
            out.append(full)
    return out


def _schema_images(v) -> list[str]:
    """schema.org image: an address, an ImageObject, or a list of either."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [u for u in (v.get("url"), v.get("contentUrl")) if isinstance(u, str)][:1]
    if isinstance(v, list):
        return [u for x in v[:5] for u in _schema_images(x)]
    return []


def kind_of(data: bytes, content_type: str) -> str | None:
    """The picture's type, if it's one we take and its first bytes agree (CC-197: never the header alone)."""
    ct = (content_type or "").split(";")[0].strip().lower()
    head = data[:16]
    agrees = {
        "image/jpeg": head.startswith(b"\xff\xd8\xff"),
        "image/png": head.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": head[:4] == b"RIFF" and head[8:12] == b"WEBP",
        "image/gif": head[:6] in (b"GIF87a", b"GIF89a"),
        "image/avif": head[4:12] in (b"ftypavif", b"ftypavis"),
    }
    return ct if agrees.get(ct) else None


def picture_from(image_url: str, page_url: str = "") -> tuple[bytes, str, bytes]:
    """Fetch one picture and make our WebP of it: (original, extension, webp). NoPicture if it isn't usable. page_url is
    the page that named it (CC-320: a picture on another server than the page has that server's own robots.txt rule)."""
    got = web.fetch_file(image_url, ACCEPT, MAX_BYTES, page_url=page_url)
    kind = kind_of(got.data, got.content_type)
    if kind is None:
        raise NoPicture(f"not a picture we take ({(got.content_type or 'no type').split(';')[0][:60]})")
    try:
        webp = event_images.normalise(got.data)
    except event_images.BadImage as e:
        raise NoPicture(f"the picture can't be read ({e})") from e
    from PIL import Image, ImageStat
    with Image.open(io.BytesIO(webp)) as im:
        if min(im.size) < MIN_SIDE:
            raise NoPicture(f"the picture is too small ({im.width}x{im.height})")
        # CC-321: a blank or single-colour picture (a placeholder, a spacer) is no picture of the event
        if max(ImageStat.Stat(im.convert("RGB")).stddev) < BLANK_STDDEV:
            raise NoPicture("the picture is blank (one colour)")
    return got.data, TYPES[kind], webp


def _used_elsewhere(db, event: Event, webp: bytes) -> bool:
    """Another event already has this exact picture: a site's logo or default share image, not this event's."""
    name = event_images.key_for(event.id, webp).rsplit("/", 1)[1]
    return db.scalar(select(Event.id).where(Event.id != event.id, Event.image_key.like(f"%/{name}")).limit(1)) is not None


def add_picture(db, event: Event, to=None) -> str:
    """Look for one event's picture on its page and store it. Returns a note for the run's log. Raises
    web.FetchError when it's worth trying again later; web.Blocked and NoPicture when it isn't."""
    if event.image_override:  # CC-265: the admin's artwork choice always wins
        raise NoPicture("picture chosen by admin")
    page = web.fetch(event.url, ai=False)  # nothing here goes to Claude
    candidates = preview_images(page.text, page.url)
    if not candidates:
        raise NoPicture("the page names no picture")
    why, failed = "the page names no picture", None
    for image_url in candidates[:3]:  # the first is nearly always it; a broken one may have a working fallback
        try:
            data, ext, webp = picture_from(image_url, page.url)
        except (NoPicture, web.Blocked) as e:
            why = str(e)
            continue
        except web.FetchError as e:
            failed = e
            continue
        if _used_elsewhere(db, event, webp):
            why = "the page's picture is the same as another event's (a logo or the site's default)"
            continue
        event_images.store_photo(event, data, ext, source_url=page.url, to=to, webp=webp)
        return f"picture stored from {urlsplit(image_url).hostname}"
    if failed is not None:  # none worked, and one might next time
        raise failed
    raise NoPicture(why)


def due(db, now: datetime, limit: int = PER_RUN) -> list[Event]:
    """Sourced, live events with no picture yet, oldest first, that haven't been tried too often or too lately."""
    return db.scalars(select(Event).where(
        Event.found_by_id.is_not(None), Event.status == EventStatus.approved, Event.image_key.is_(None), Event.url != "",
        Event.picture_tries < MAX_TRIES, Event.image_override.is_(False),
        or_(Event.picture_checked_at.is_(None), Event.picture_checked_at < now - RETRY_AFTER),
    ).order_by(Event.id).limit(limit)).all()


def add_pictures(db, now: datetime | None = None, limit: int = PER_RUN, to=None) -> list[tuple[str, str, str]]:
    """Look for the pictures that are due, committing after each. Returns (source's name, status, note) for each:
    status is ok (stored), none (no usable picture, not tried again) or error (tried again tomorrow)."""
    now = now or utcnow()
    done = []
    for event in due(db, now, limit):
        event.picture_tries += 1
        event.picture_checked_at = now
        db.commit()  # CC-319: the try is counted before the page is fetched, so a look that is killed part-way still used one up
        try:
            status, note = "ok", add_picture(db, event, to)
        except (NoPicture, web.Blocked) as e:
            status, note = "none", str(e)
            event.picture_tries = MAX_TRIES
        except web.FetchError as e:
            status, note = "error", f"{e} (try {event.picture_tries} of {MAX_TRIES})"
        except Exception as e:  # noqa: BLE001 - one event's picture must not stop the rest, or discovery
            status, note = "error", f"{type(e).__name__}: {e}"[:300]
        db.commit()
        source = db.get(DiscoverySource, event.found_by_id) if event.found_by_id else None
        done.append((source.name if source else "A deleted source", status, f"{event.title}: {note}"))
    return done


# --- CC-310: a found item's candidate picture ---------------------------------------------------------------------

TICKET_PAGES = 2  # tickets or calendar links tried when the event's own page has no usable picture
ITEM_PER_RUN = 10
NOTE_MAX = 300


def item_owner(item: DiscoveredItem) -> str:
    """The folder a candidate's files go in until it has an event (its key is kept when the event is made)."""
    return f"found-{item.id}"


def _item_dup(db, item: DiscoveredItem, webp: bytes) -> bool:
    """Another event, or another found item still waiting, already has this exact picture (a logo or a default share image)."""
    name = event_images.key_for(item_owner(item), webp).rsplit("/", 1)[1]
    if db.scalar(select(Event.id).where(Event.image_key.like(f"%/{name}")).limit(1)) is not None:
        return True
    return db.scalar(select(DiscoveredItem.id).where(DiscoveredItem.id != item.id, DiscoveredItem.status == "new",
                                                     DiscoveredItem.image_key.like(f"%/{name}")).limit(1)) is not None


def item_dup_for_event(db, event: Event, key: str) -> bool:
    """On approval: the item's picture was taken by another event since it was found."""
    name = key.rsplit("/", 1)[1]
    return db.scalar(select(Event.id).where(Event.id != event.id, Event.image_key.like(f"%/{name}")).limit(1)) is not None


GENERIC = "The page names no picture"


def _store_first(db, item: DiscoveredItem, candidates: list[str], page_url: str, to, how: str = "",
                 source_url: str = "") -> tuple[str | None, str, web.FetchError | None]:
    """The first usable picture of these addresses: (note when stored, else None; why not; a fetch error worth retrying).
    CC-321: source_url is the page the picture came from (default: page_url), how says which way it was found."""
    why, failed = GENERIC, None
    for image_url in candidates[:3]:
        try:
            data, ext, webp = picture_from(image_url, page_url)
        except web.Blocked as e:
            why = f"Refused: {e}"
            continue
        except NoPicture as e:
            why = str(e)[:1].upper() + str(e)[1:]
            continue
        except web.FetchError as e:
            failed = e
            continue
        if _item_dup(db, item, webp):
            why = "The picture is the same as another event's (a logo or the site's default)"
            continue
        _keep_picture(db, item, data, ext, webp, source_url or page_url, to)
        return f"picture stored from {urlsplit(image_url).hostname}{how}", "", None
    return None, why, failed


def _try_pictures(db, item: DiscoveredItem, page, to) -> tuple[str | None, str, web.FetchError | None]:
    """The first usable preview picture of one page: (note when stored, else None; why not; a fetch error worth retrying)."""
    candidates = preview_images(page.text, page.url)
    if not candidates:
        return None, GENERIC, None
    return _store_first(db, item, candidates, page.url, to)


def _keep_picture(db, item: DiscoveredItem, data: bytes, ext: str, webp: bytes, source_url: str, to) -> None:
    # the fetches before this can take minutes: an admin may have uploaded or removed a picture meanwhile (the session
    # keeps what it loaded), so what the database says now decides
    db.refresh(item, ["image_override", "status"])
    if item.image_override or item.status != "new":
        raise NoPicture("picture chosen by admin")
    item.image_key = event_images.save(item_owner(item), data, ext, to, webp)
    item.image_source_url = source_url


def _aic_picture(db, item: DiscoveredItem, to) -> str:
    """CC-320: the Art Institute's pages time out, but its own API (already read for its events) gives the picture. It goes
    through the same picture rules as any other: fetch_file, size and type, our own WebP, no duplicates."""
    try:
        image_url = aic_image_url(item.url)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):  # the API no longer has it
            raise NoPicture("The Art Institute's API no longer has this one") from e
        raise web.FetchError(f"the Art Institute's API answered HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError: an answer that isn't JSON
        raise web.FetchError(f"the Art Institute's API didn't answer ({type(e).__name__})") from e
    if not image_url:
        raise NoPicture("The Art Institute's API has no picture for this one")
    try:
        data, ext, webp = picture_from(image_url, item.url)
    except web.Blocked as e:
        raise NoPicture(f"Refused: {e}") from e
    except NoPicture as e:
        raise NoPicture(str(e)[:1].upper() + str(e)[1:]) from e
    if _item_dup(db, item, webp):
        raise NoPicture("The picture is the same as another event's (a logo or the site's default)")
    _keep_picture(db, item, data, ext, webp, item.url, to)
    return f"picture stored from {urlsplit(image_url).hostname} (the Art Institute's API)"


class _Listing:
    """One source's listing page, read once per run: its cards and the pictures several events would share."""
    def __init__(self, url: str, cards: list[htmlpics.Card]):
        self.url, self.cards, self.shared = url, cards, htmlpics.shared_images(cards)


def _listing_of(source: DiscoverySource, cache: dict) -> _Listing | None:
    """CC-321: the source's listing page, fetched at most once per run however many of its events look for a picture
    (a failure is remembered too). Only its first page: one extra fetch per source per run. Same fetch rules as any page."""
    if source.id in cache:
        return cache[source.id]
    cache[source.id] = None
    if source.pages:
        try:
            page = web.fetch(source.pages[0], ai=False)
        except (web.Blocked, web.FetchError):
            return None
        if not htmlpics.looks_missing(page.text):
            cache[source.id] = _Listing(page.url, htmlpics.listing_cards(page.text, page.url))
    return cache[source.id]


def _has_own_page(item: DiscoveredItem, source: DiscoverySource) -> bool:
    """A page of its own on the source's site that isn't the listing page itself (CC-321)."""
    if not item.url or not on_source_site(item.url, source):
        return False
    here = item.url.split("#")[0].rstrip("/")
    return not any(p.split("#")[0].rstrip("/") == here for p in source.pages or [])


def _card_picture(db, item: DiscoveredItem, listing: _Listing | None, to) -> tuple[str | None, str, web.FetchError | None]:
    """CC-321: the picture on the event's card on the listing page, when the card is the event's (its link or its heading)."""
    if listing is None:
        return None, GENERIC, None
    card = htmlpics.card_for(listing.cards, item.title, item.url if item.url else "")
    if card is None:
        return None, GENERIC, None
    if card.image in listing.shared:
        return None, "The card's picture is shared by other events on the listing page", None
    return _store_first(db, item, [card.image], listing.url, to, " (the event's card on the listing page)")


def _main_picture(db, item: DiscoveredItem, page, listing: _Listing | None, to) -> tuple[str | None, str, web.FetchError | None]:
    """CC-321: the largest picture in the event page's main content, unless another event's card on the listing has it too."""
    candidates = [u for u in htmlpics.main_picture(page.text, page.url) if listing is None or u not in listing.shared]
    if not candidates:
        return None, GENERIC, None
    return _store_first(db, item, candidates[:1], page.url, to, " (the main picture on the event's page)")


def add_item_picture(db, item: DiscoveredItem, source: DiscoverySource, to=None, cache: dict | None = None) -> str:
    """Look for a found item's picture: the preview tags of its own page, then its card on the source's listing page (CC-321),
    then the main picture of its own page (CC-321), then the tickets or calendar pages it links to. Returns a note for the
    run's log and sets item.image_key. Raises web.FetchError when it's worth trying again later, and NoPicture (with the
    reason for the admin) when it isn't. Every fetch is web.fetch's: public hosts, robots.txt, spacing. cache is the run's:
    the listing page is fetched once for all its events."""
    if item.image_override:
        raise NoPicture("picture chosen by admin")
    if source.reader == "aic_api" and item.url and on_source_site(item.url, source):  # CC-320
        return _aic_picture(db, item, to)
    cache = {} if cache is None else cache
    own = _has_own_page(item, source)
    page, refused, failed = None, "", None
    if own:
        try:
            page = web.fetch(item.url, ai=False)  # nothing here goes to Claude
        except web.Blocked as e:
            refused = f"Refused: {e}"
        except web.FetchError as e:  # CC-321: the card can still help; the error stands if it doesn't
            failed = e
    if page is not None and htmlpics.looks_missing(page.text):  # the site's own "not found" page, answered with a 200
        raise NoPicture("Page not found: the site's own not-found page")
    done, why, err = (None, GENERIC, None) if page is None else _try_pictures(db, item, page, to)
    if done:
        return done
    failed = failed or err
    listing = _listing_of(source, cache) if source.pages else None
    done, more, err = _card_picture(db, item, listing, to)
    if done:
        return done
    failed, why = failed or err, (more if why == GENERIC else why)
    if page is not None:
        done, more, err = _main_picture(db, item, page, listing, to)
        if done:
            return done
        failed, why = failed or err, (more if why == GENERIC else why)
        for link, _ in links_of(page.text, page.url)[:TICKET_PAGES]:
            # CC-320: a fallback can only add a picture. What it fails with never replaces the reason for the event's own page
            try:
                tpage = web.fetch(link, ai=False)
            except (web.Blocked, web.FetchError):
                continue
            done, _more, _err = _try_pictures(db, item, tpage, to)
            if done:
                return done
    if failed is not None:
        raise failed
    if refused:
        raise NoPicture(refused)
    if not own and why == GENERIC:
        raise NoPicture("The event has no page of its own on the source's site")
    raise NoPicture(why)


def due_items(db, now: datetime, limit: int = ITEM_PER_RUN) -> list[DiscoveredItem]:
    return db.scalars(select(DiscoveredItem).where(
        DiscoveredItem.status == "new", DiscoveredItem.image_key.is_(None),
        DiscoveredItem.image_override.is_(False),
        # CC-320: out of tries, but looked at by an older finder (which had the bugs) -> one more look, in the hourly runs
        or_(DiscoveredItem.image_tries < MAX_TRIES, DiscoveredItem.image_finder < FINDER_VERSION),
        or_(DiscoveredItem.image_checked_at.is_(None), DiscoveredItem.image_checked_at < now - RETRY_AFTER_ITEM),
        ~DiscoveredItem.image_note.in_(NO_RELOOK),
    ).order_by(and_(DiscoveredItem.image_checked_at.is_not(None), DiscoveredItem.image_finder < FINDER_VERSION),
               DiscoveredItem.id).limit(limit)).all()  # CC-320: items never looked at, and those mid-tries, go before the extra looks


# CC-320: reasons that no finder fix changes, so an older finder's look isn't repeated for them
NO_RELOOK = ("The source was deleted",)  # CC-321: "no page of its own" is looked at once more: its card may have the picture
RETRY_AFTER_ITEM = timedelta(hours=1)  # the hourly run: a found item's picture is wanted before its review, not tomorrow


def look_for_item(db, item: DiscoveredItem, now: datetime, to=None, cache: dict | None = None) -> tuple[str, str, str]:
    """One found item's picture look, committed: (source's name, status, note), as add_pictures gives them."""
    source = db.get(DiscoverySource, item.source_id) if item.source_id else None
    if item.image_finder < FINDER_VERSION:  # CC-320: stamped before the fetch, so a look that is killed isn't repeated
        if item.image_tries >= MAX_TRIES:  # out of tries under an older finder: this look is the one more it is owed
            item.image_tries = MAX_TRIES - 1
        item.image_finder = FINDER_VERSION
    item.image_tries += 1
    item.image_checked_at = now
    db.refresh(item, ["image_override"])  # an admin may have chosen since the list was made (the session keeps what it loaded)
    if not item.image_override:  # CC-319: counted and committed before the fetch. If the look is killed, this is the reason left
        item.image_note = (f"The last look didn't finish (try {item.image_tries} of {MAX_TRIES})" if item.image_tries < MAX_TRIES
                           else f"Gave up after {MAX_TRIES} tries: the look didn't finish (a very large picture?)")
    db.commit()
    try:
        if source is None:
            raise NoPicture("The source was deleted")
        status, note = "ok", add_item_picture(db, item, source, to, cache)
        item.image_note = ""
    except (NoPicture, web.Blocked) as e:
        status, note = "none", str(e)
        item.image_tries = MAX_TRIES
        if not item.image_override:  # an admin's choice has no reason to give
            item.image_note = note[:NOTE_MAX]
    except web.FetchError as e:
        status, note = "error", f"{e} (try {item.image_tries} of {MAX_TRIES})"
        item.image_note = (f"Still trying: {e} (try {item.image_tries} of {MAX_TRIES})" if item.image_tries < MAX_TRIES
                           else f"Gave up after {MAX_TRIES} tries: {e}")[:NOTE_MAX]
    except Exception as e:  # noqa: BLE001 - one item's picture must not stop the rest, or discovery
        status, note = "error", f"{type(e).__name__}: {e}"[:300]
        item.image_note = (f"Still trying (try {item.image_tries} of {MAX_TRIES})" if item.image_tries < MAX_TRIES
                           else f"Gave up after {MAX_TRIES} tries")
    if item.image_override:  # an admin's choice has no reason to give: not even the one set before the fetch (CC-319)
        item.image_note = ""
    db.commit()
    return (source.name if source else "A deleted source", status, f"{item.title}: {note}")


def add_item_pictures(db, now: datetime | None = None, limit: int = ITEM_PER_RUN, to=None) -> list[tuple[str, str, str]]:
    """Look for the pictures of found items waiting for review, committing after each. Same results as add_pictures."""
    now = now or utcnow()
    cache: dict = {}  # CC-321: one listing page per source for the whole run
    return [look_for_item(db, item, now, to, cache) for item in due_items(db, now, limit)]


# --- CC-318: the one-off catch-up for items already on the review list ---------------------------------------------

def backlog(db, retry_missing: bool = False) -> dict[str, list[DiscoveredItem]]:
    """The review list (status new) sorted for the backfill, oldest first:
    todo (to look at now), has_picture, admin_choice (never touched), reason (a note is recorded, a reason or a try
    still going: skipped unless retry_missing, and the hourly run finishes the tries). An item with no page address is
    todo too: its look fetches nothing and records why there's no picture."""
    out: dict[str, list[DiscoveredItem]] = {"todo": [], "has_picture": [], "admin_choice": [], "reason": []}
    for item in db.scalars(select(DiscoveredItem).where(DiscoveredItem.status == "new").order_by(DiscoveredItem.id)):
        if item.image_override:
            out["admin_choice"].append(item)
        elif item.image_key:
            out["has_picture"].append(item)
        elif item.image_note and not retry_missing:  # CC-318: a re-run changes nothing
            out["reason"].append(item)
        else:
            out["todo"].append(item)
    return out


def _still_todo(item: DiscoveredItem, retry_missing: bool, started: datetime) -> bool:
    """The item as the database has it now: the list was made a while ago, and an admin or the hourly run (a separate
    process, with its own spacing between requests) may have got there first."""
    checked = item.image_checked_at
    if checked is not None and checked.tzinfo is None:  # SQLite hands back naive times; the column is UTC
        checked = checked.replace(tzinfo=UTC)
    return (item.status == "new" and not item.image_override and not item.image_key
            and (retry_missing or not item.image_note) and (checked is None or checked <= started))


def backfill_items(db, retry_missing: bool = False, limit: int | None = None, now: datetime | None = None,
                   to=None, on_result=None) -> list[tuple[str, str, str]]:
    """Look for a picture for each review-list item that has none and no note. Never touches an item with a picture,
    an admin's choice or a note, so running it again finds nothing. Each item is read afresh just before its look and
    skipped if it changed since the list was made. Uses look_for_item: the same fetch rules, spacing and robots.txt as
    the hourly run, and no Claude call. With retry_missing, an item whose last look gave a reason gets a fresh set of
    tries. on_result(row) is called after each item, so a long run shows its progress."""
    started = now = now or utcnow()
    done, cache = [], {}
    for item in backlog(db, retry_missing)["todo"][:limit]:
        db.refresh(item)
        if not _still_todo(item, retry_missing, started):
            continue
        if retry_missing and item.image_tries >= MAX_TRIES:
            item.image_tries = 0
        row = look_for_item(db, item, now, to, cache)
        done.append(row)
        if on_result:
            on_result(row)
    return done
