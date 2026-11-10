"""CC-248: a member's own journal events (ones not in the catalog) and their own categories.

Everything here is private to the member: only they can read it, admins included, and nothing is shared with
crews or shown on a public page. An own event has a title and a date (past or future: an upcoming one shows in
My week on its date), and the rest of a journal entry can come later. Its photo is checked by Rekognition with
the usual rule but has no approver, since only its member sees it.
"""
import logging
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from starlette.concurrency import run_in_threadpool

from .. import photos
from ..deps import DB, CurrentUser, require_verified_email
from ..models import JournalCategory, OwnEvent
from ..ratelimit import SlidingWindowLimiter
from ..schemas import OwnCategoryIn, OwnCategoryOut, OwnEventIn, OwnEventOut

router = APIRouter(prefix="/me", tags=["me"])
log = logging.getLogger(__name__)

MAX_EVENTS = 500
MAX_CATEGORIES = 20
WRITES_PER_HOUR = 300  # adding and changing events and categories: far above anyone keeping a journal
EARLIEST = date(1950, 1, 1)
write_limiter = SlidingWindowLimiter(window_seconds=3600)


def _writing(user) -> None:
    if not write_limiter.hit(str(user.id), WRITES_PER_HOUR):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of changes at once. Try again later.")


def _own_event(db, user, own_id: int) -> OwnEvent:
    own = db.get(OwnEvent, own_id)
    if own is None or own.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return own


def _own_category(db, user, category_id: int) -> JournalCategory:
    cat = db.get(JournalCategory, category_id)
    if cat is None or cat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")
    return cat


def _out(own: OwnEvent) -> OwnEventOut:
    out = OwnEventOut.model_validate(own)
    out.category_name = own.own_category.name if own.own_category else (own.category or "")
    out.photo_url = photos.own_link(own) if own.photo_key else ""
    return out


# ---- own events ---------------------------------------------------------------------------

@router.get("/own-events", response_model=list[OwnEventOut])
def list_own_events(db: DB, user: CurrentUser):
    """Your own journal events, latest date first."""
    rows = db.scalars(select(OwnEvent).where(OwnEvent.user_id == user.id)
                      .order_by(OwnEvent.on_date.desc(), OwnEvent.id.desc())).all()
    return [_out(r) for r in rows]


def _apply(db, user, own: OwnEvent, body: OwnEventIn) -> None:
    if body.date < EARLIEST or body.date > date.today() + timedelta(days=730):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a date between 1950 and two years from now")
    if body.category_id is not None:
        _own_category(db, user, body.category_id)
    own.title, own.on_date, own.venue = body.title, body.date, body.venue
    own.category, own.category_id = body.category, body.category_id
    own.words, own.review, own.recommend = body.words, body.review, body.recommend


@router.post("/own-events", response_model=OwnEventOut, status_code=status.HTTP_201_CREATED)
def add_own_event(body: OwnEventIn, db: DB, user: CurrentUser):
    """Add an event that isn't in the catalog to your journal."""
    _writing(user)
    if db.scalar(select(func.count()).select_from(OwnEvent).where(OwnEvent.user_id == user.id)) >= MAX_EVENTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"You can keep up to {MAX_EVENTS} of your own events")
    own = OwnEvent(user_id=user.id)
    _apply(db, user, own, body)
    db.add(own)
    db.commit()
    db.refresh(own)
    return _out(own)


@router.put("/own-events/{own_id}", response_model=OwnEventOut)
def change_own_event(own_id: int, body: OwnEventIn, db: DB, user: CurrentUser):
    """Change one of your own events: all of it, as the form sends it."""
    _writing(user)
    own = _own_event(db, user, own_id)
    _apply(db, user, own, body)
    db.commit()
    db.refresh(own)
    return _out(own)


@router.delete("/own-events/{own_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_own_event(own_id: int, db: DB, user: CurrentUser):
    """Delete one of your own events, and its photo. Idempotent."""
    own = db.get(OwnEvent, own_id)
    if own is not None and own.user_id == user.id:
        photos.remove_own(own)
        db.delete(own)
        db.commit()


def _own_photo_saved(db, user, own_id: int, data: bytes, content_type: str) -> OwnEvent:
    own = _own_event(db, user, own_id)
    if photos.uploads_today(db, user.id) >= photos.UPLOADS_PER_DAY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's 10 photos today. Try again tomorrow.")
    kind = photos.kind_of(data, content_type)
    if kind is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Send a JPEG or WebP photo.")
    try:
        photos.save_own(db, own, data, kind)
    except photos.Refused:
        db.commit()  # the check counts toward the limit; the photo was never stored
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This photo can't be used. Try another.") from None
    except Exception:  # Rekognition or storage unreachable: nothing was stored
        db.rollback()
        log.exception("Couldn't check or store an own event's photo")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Photos can't be sent right now. Try again later.") from None
    db.commit()
    db.refresh(own)
    return own


@router.put("/own-events/{own_id}/photo", response_model=OwnEventOut)
async def add_own_photo(own_id: int, request: Request, db: DB, user: CurrentUser):
    """Add (or replace) the photo for one of your own events: the body is the photo, JPEG or WebP, as the
    browser shrank it. Rekognition checks it; there's no approver, so it shows at once (to you only)."""
    require_verified_email(user, "sending photos")
    if int(request.headers.get("content-length") or 0) > photos.MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "That photo is too big. Try a smaller one.")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > photos.MAX_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "That photo is too big. Try a smaller one.")
    own = await run_in_threadpool(_own_photo_saved, db, user, own_id, bytes(data), request.headers.get("content-type", ""))
    return _out(own)


@router.delete("/own-events/{own_id}/photo", response_model=OwnEventOut)
def remove_own_photo(own_id: int, db: DB, user: CurrentUser):
    own = _own_event(db, user, own_id)
    photos.remove_own(own)
    db.commit()
    db.refresh(own)
    return _out(own)


# ---- own categories -----------------------------------------------------------------------

def _counts(db, user) -> dict[int, int]:
    return dict(db.execute(select(OwnEvent.category_id, func.count()).where(
        OwnEvent.user_id == user.id, OwnEvent.category_id.is_not(None)).group_by(OwnEvent.category_id)).all())


def _cat_out(cat: JournalCategory, counts: dict[int, int]) -> OwnCategoryOut:
    out = OwnCategoryOut.model_validate(cat)
    out.events = counts.get(cat.id, 0)
    return out


def _name_free(db, user, name: str, but: int | None = None) -> None:
    """Names are unique per member, whatever their case."""
    q = select(JournalCategory.id).where(JournalCategory.user_id == user.id, func.lower(JournalCategory.name) == name.lower())
    if but is not None:
        q = q.where(JournalCategory.id != but)
    if db.scalar(q) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "You already have a category with that name")


@router.get("/categories", response_model=list[OwnCategoryOut])
def list_categories(db: DB, user: CurrentUser):
    """Your own categories, by name, each with how many of your events are in it."""
    counts = _counts(db, user)
    rows = db.scalars(select(JournalCategory).where(JournalCategory.user_id == user.id)
                      .order_by(func.lower(JournalCategory.name))).all()
    return [_cat_out(c, counts) for c in rows]


@router.post("/categories", response_model=OwnCategoryOut, status_code=status.HTTP_201_CREATED)
def add_category(body: OwnCategoryIn, db: DB, user: CurrentUser):
    _writing(user)
    if db.scalar(select(func.count()).select_from(JournalCategory).where(JournalCategory.user_id == user.id)) >= MAX_CATEGORIES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"You can have up to {MAX_CATEGORIES} of your own categories")
    _name_free(db, user, body.name)
    cat = JournalCategory(user_id=user.id, name=body.name)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return _cat_out(cat, {})


@router.put("/categories/{category_id}", response_model=OwnCategoryOut)
def rename_category(category_id: int, body: OwnCategoryIn, db: DB, user: CurrentUser):
    _writing(user)
    cat = _own_category(db, user, category_id)
    _name_free(db, user, body.name, but=cat.id)
    cat.name = body.name
    db.commit()
    db.refresh(cat)
    return _cat_out(cat, _counts(db, user))


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(category_id: int, db: DB, user: CurrentUser):
    """Delete one of your categories. Its events stay in your journal, with no category. Idempotent."""
    cat = db.get(JournalCategory, category_id)
    if cat is not None and cat.user_id == user.id:
        # SQLite in tests doesn't enforce ON DELETE SET NULL, so it's done here too
        db.execute(update(OwnEvent).where(OwnEvent.category_id == cat.id).values(category_id=None))
        db.delete(cat)
        db.commit()
