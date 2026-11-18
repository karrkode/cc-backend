"""CC-209: suggest a venue (members) and review the suggestions (admins). See app/venuesuggestions.py."""
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from .. import venuesuggestions as vs
from ..clock import today_in_chicago, utcnow
from ..deps import DB, AdminUser, CurrentUser, require_verified_email
from ..discovery.fetch import FetchError, PrivateAddress, check_public
from ..models import DiscoverySource, SourceKind, Venue, VenueSuggestion
from ..schemas import (VenueSuggestionAccept, VenueSuggestionAdminOut, VenueSuggestionDecline, VenueSuggestionIn, VenueSuggestionOut)
from urllib.parse import urlsplit

router = APIRouter(tags=["venue-suggestions"])


@router.post("/venue-suggestions", response_model=VenueSuggestionOut, status_code=status.HTTP_201_CREATED)
def suggest_venue(body: VenueSuggestionIn, db: DB, user: CurrentUser):
    require_verified_email(user, "suggesting a venue")  # before the limit, so it uses none
    # CC-209: every attempt counts, before any lookup, so a member can't make the server resolve hosts or probe
    # which pages are already sources; the 5 a day then counts what was saved
    if not vs.minute_limiter.hit(str(user.id), vs.PER_MINUTE) or not vs.day_limiter.hit(str(user.id), 5 * vs.PER_DAY):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of links at once. Wait a minute and try again.")
    if vs.over_limit(db, user):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's today's suggestions. Thank you! Try again tomorrow.")
    url = vs.clean_url(body.url)
    if vs.read_already(db, url):
        raise HTTPException(status.HTTP_409_CONFLICT, vs.ALREADY)
    row = VenueSuggestion(user_id=user.id, url=url, venue_name=body.venue_name, note=body.note)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/venue-suggestions/mine", response_model=list[VenueSuggestionOut])
def my_suggestions(db: DB, user: CurrentUser):
    return db.scalars(select(VenueSuggestion).where(VenueSuggestion.user_id == user.id)
                      .order_by(VenueSuggestion.created_at.desc(), VenueSuggestion.id.desc()).limit(50)).all()


def _admin_out(row: VenueSuggestion) -> VenueSuggestionAdminOut:
    out = VenueSuggestionAdminOut.model_validate(row)
    out.suggested_by = row.user.display_name if row.user else ""
    return out


@router.get("/admin/venue-suggestions", response_model=list[VenueSuggestionAdminOut])
def list_suggestions(db: DB, _: AdminUser, status_: str = "pending"):
    q = select(VenueSuggestion).order_by(VenueSuggestion.created_at.desc(), VenueSuggestion.id.desc()).limit(200)
    pending = status_ not in ("all", "accepted", "declined")
    if status_ != "all":
        q = q.where(VenueSuggestion.status == ("pending" if pending else status_))
    rows = list(db.scalars(q))
    out = [_admin_out(r) for r in rows]
    if pending:
        # CC-210: the sites more members have suggested come first (ties: oldest first). A page can only be suggested
        # once (CC-209), so "the same venue" means the same site; the admin sees the count and can add the best page.
        who: dict[str, set] = {}
        for r in rows:
            who.setdefault(_site(r.url), set()).add(r.user_id if r.user_id is not None else f"gone-{r.id}")
        for o in out:
            o.same_site = len(who[_site(o.url)])
        out.sort(key=lambda o: (-o.same_site, o.created_at, o.id))
    return out


def _site(url: str) -> str:
    return vs._parts(url)[0]


def _pending(db: DB, suggestion_id: int) -> VenueSuggestion:
    row = db.get(VenueSuggestion, suggestion_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Suggestion not found")
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already {row.status}")
    return row


@router.post("/admin/venue-suggestions/{suggestion_id}/accept", response_model=VenueSuggestionAdminOut)
def accept(suggestion_id: int, body: VenueSuggestionAccept, db: DB, _: AdminUser):
    """Add an event source that reads the suggested link, due at the next hourly run."""
    row = _pending(db, suggestion_id)
    try:
        check_public(row.url)  # addresses change: check again before it's ever read
    except (PrivateAddress, FetchError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"That link can't be read: {e}.") from e
    others = db.scalars(select(DiscoverySource)).all()  # a source may have been added since it was suggested
    if any(vs._parts(p) == vs._parts(row.url) for o in others for p in (o.pages or [])):
        raise HTTPException(status.HTTP_409_CONFLICT, "A source already reads that page.")
    venue = None
    if body.venue_id is not None:
        venue = db.get(Venue, body.venue_id)
        if venue is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "No venue with that venue_id")
    name = (body.name.strip() or row.venue_name or (venue.name if venue else "") or (urlsplit(row.url).hostname or "").removeprefix("www.")).strip()[:200]
    if db.scalar(select(DiscoverySource.id).where(DiscoverySource.name == name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A source with that name exists. Give this one another name.")
    source = DiscoverySource(name=name, kind=SourceKind(body.kind), pages=[row.url], reader="claude", venue=venue,
                             venue_name=venue.name if venue else name, next_check_on=today_in_chicago())
    db.add(source)
    db.flush()
    row.status, row.source_id, row.decided_at = "accepted", source.id, utcnow()
    db.commit()
    db.refresh(row)
    return _admin_out(row)


@router.post("/admin/venue-suggestions/{suggestion_id}/decline", response_model=VenueSuggestionAdminOut)
def decline(suggestion_id: int, body: VenueSuggestionDecline, db: DB, _: AdminUser):
    row = _pending(db, suggestion_id)
    row.status, row.decline_reason, row.decided_at = "declined", body.reason.strip(), utcnow()
    db.commit()
    db.refresh(row)
    return _admin_out(row)
