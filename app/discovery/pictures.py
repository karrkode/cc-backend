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
- A tiny picture (an icon), or one another event already has (a site's logo or default share image), isn't used.
"""
import io
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from urllib.parse import urljoin, urlsplit

from sqlalchemy import or_, select

from .. import event_images
from ..models import DiscoverySource, Event, EventStatus
from . import fetch as web
from .jsonld import _blocks, _is_event, _objects, _tags

MAX_TRIES = 3
RETRY_AFTER = timedelta(hours=20)  # about a day: the hourly run looks again tomorrow, not next hour
PER_RUN = 10
MAX_BYTES = 10_000_000  # a share image is usually well under 1 MB
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


def picture_from(image_url: str) -> tuple[bytes, str, bytes]:
    """Fetch one picture and make our WebP of it: (original, extension, webp). NoPicture if it isn't usable."""
    got = web.fetch_file(image_url, ACCEPT, MAX_BYTES)
    kind = kind_of(got.data, got.content_type)
    if kind is None:
        raise NoPicture(f"not a picture we take ({(got.content_type or 'no type').split(';')[0][:60]})")
    try:
        webp = event_images.normalise(got.data)
    except event_images.BadImage as e:
        raise NoPicture(f"the picture can't be read ({e})") from e
    from PIL import Image
    with Image.open(io.BytesIO(webp)) as im:
        if min(im.size) < MIN_SIDE:
            raise NoPicture(f"the picture is too small ({im.width}x{im.height})")
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
            data, ext, webp = picture_from(image_url)
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
    now = now or datetime.now(UTC)
    done = []
    for event in due(db, now, limit):
        event.picture_tries += 1
        event.picture_checked_at = now
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
