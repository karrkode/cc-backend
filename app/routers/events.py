from datetime import UTC, date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import case, func, or_, select

from ..areas import area_of
from ..config import get_settings
from ..deps import DB, AdminUser, CurrentUser, OptionalUser
from ..models import Area, Event, EventStatus, Plan, PlanStatus, Role, Venue
from ..picks import attach_picks, pick_condition
from ..schemas import EventFacets, EventIn, EventOut, EventPage, EventPatch, VenueIn
from ..visibility import live_condition, utcnow, visible_to

router = APIRouter(prefix="/events", tags=["events"])


def get_or_create_venue(db: DB, v: VenueIn) -> Venue:
    name, hood = v.name.strip(), v.neighborhood.strip()
    venue = db.scalar(select(Venue).where(func.lower(Venue.name) == name.lower(),
                                          func.lower(Venue.neighborhood) == hood.lower()))
    if venue is None:
        venue = Venue(name=name, neighborhood=hood, area=v.area or area_of(hood))
        db.add(venue)
        db.flush()
    return venue


Sort = Literal["start", "closing", "opening", "az", "price"]


def _order(sort: Sort, today: date):
    """ORDER BY for each sort the site offers; every one ends in a stable tie-break."""
    closing = (Event.end_date.asc().nulls_last(), Event.sort_title, Event.id)
    if sort == "closing":  # soonest to close first; open runs and no end date last
        return closing
    if sort == "opening":  # not open yet, soonest first; then everything already running, by closing
        upcoming = case((Event.begin_date > today, Event.begin_date), else_=None)
        return (upcoming.asc().nulls_last(), *closing)
    if sort == "az":
        return (Event.sort_title, Event.id)
    if sort == "price":  # cheapest first; "price not posted" last
        return (Event.min_price.asc().nulls_last(), *closing)
    return (Event.begin_date.asc().nulls_first(), Event.title, Event.id)  # start: TBD first, as the old site


@router.get("", response_model=EventPage)
def list_events(
    db: DB,
    q: Annotated[str | None, Query(max_length=100, description="Search titles, venues and neighborhoods")] = None,
    area: Annotated[list[Area] | None, Query()] = None,
    hood: Annotated[list[str] | None, Query(description="Only these neighborhoods (exact names)")] = None,
    category: Annotated[list[str] | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from", description="Running on or after this date")] = None,
    date_to: Annotated[date | None, Query(alias="to", description="Running on or before this date")] = None,
    ended_before: Annotated[date | None, Query(description="Only events that closed before this date")] = None,
    max_price: Annotated[Decimal | None, Query(ge=0, description="0 = free only")] = None,
    include_past: bool = False,
    mine: Annotated[list[PlanStatus] | None, Query(description="Only events you're interested in or going to (needs sign-in)")] = None,
    picks: Annotated[list[PlanStatus] | None, Query(description="Only the owner's picks of these kinds (the dolphins)")] = None,
    user: OptionalUser = None,
    sort: Sort = "start",
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Live events (approved, and past any scheduled publish time).

    Dates are the caller's: the site sends its own local "today" in from= / ended_before=
    (with include_past=true), so a Chicago evening isn't already "tomorrow" on a UTC server.
    sort: start (TBD first, then by opening date), closing, opening, az, price.
    mine (CC-66): only your own plans of those kinds; it never shows anyone else's.
    picks (CC-66): only the owner's public picks of those kinds (the dolphin filter buttons)."""
    conds = [live_condition()]
    if mine:
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to filter by your plans")
        conds.append(Event.id.in_(select(Plan.event_id).where(Plan.user_id == user.id, Plan.status.in_(mine))))
    if picks:
        conds.append(pick_condition(picks))
    if q:
        conds.append(or_(Event.title.icontains(q, autoescape=True), Venue.name.icontains(q, autoescape=True),
                         Venue.neighborhood.icontains(q, autoescape=True)))
    if area:
        conds.append(Venue.area.in_(area))
    if hood:
        conds.append(Venue.neighborhood.in_(hood))
    if category:
        conds.append(Event.category.in_(category))
    if ended_before:
        conds.append(Event.end_date < ended_before)
    elif not include_past:
        conds.append(or_(Event.end_date.is_(None), Event.end_date >= date.today()))
    if date_from:  # still running on date_from
        conds.append(or_(Event.end_date.is_(None), Event.end_date >= date_from))
    if date_to:  # already started by date_to
        conds.append(or_(Event.begin_date.is_(None), Event.begin_date <= date_to))
    if max_price is not None:
        conds.append(Event.min_price <= max_price)  # "price not posted" (NULL) drops out

    total = db.scalar(select(func.count(Event.id)).join(Venue).where(*conds))
    rows = db.scalars(select(Event).join(Venue).where(*conds)
                      .order_by(*_order(sort, date_from or date.today()))
                      .limit(limit).offset(offset)).all()
    return EventPage(items=attach_picks(db, rows), total=total, limit=limit, offset=offset)


@router.get("/facets", response_model=EventFacets)
def facets(db: DB, today: Annotated[date | None, Query(description="The caller's local date")] = None):
    """What the site needs to build its filters and headline counts, without loading every event:
    the categories and neighborhoods in use, and how many events there are."""
    today = today or date.today()
    live = live_condition()
    categories = db.scalars(select(Event.category).where(live).group_by(Event.category)
                            .order_by(func.count(Event.id).desc(), Event.category)).all()
    hoods = db.execute(select(Venue.neighborhood, Venue.area).join(Event).where(live, Venue.neighborhood != "")
                       .group_by(Venue.neighborhood, Venue.area).order_by(Venue.neighborhood)).all()
    current = db.scalar(select(func.count(Event.id)).where(live, or_(Event.end_date.is_(None), Event.end_date >= today)))
    return EventFacets(categories=categories, neighborhoods=[{"name": n, "area": a} for n, a in hoods],
                       current=current, total=db.scalar(select(func.count(Event.id)).where(live)))


@router.get("/lookup", response_model=list[EventOut])
def lookup(db: DB, ids: Annotated[list[str], Query(max_length=10, description="Event ids, or the old site's ids")]):
    """Live events by id, for shared outing links. Old-site links carry 6-character sheet ids,
    which match source_id; unknown ids are left out. Closed events are included."""
    numbers = [int(i) for i in ids if i.isdigit()]
    return attach_picks(db, db.scalars(select(Event).where(live_condition(), or_(Event.id.in_(numbers), Event.source_id.in_(ids)))
                                       .order_by(Event.id)).all())


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: int, db: DB, user: OptionalUser):
    event = db.get(Event, event_id)
    if event is None or not visible_to(event, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return attach_picks(db, [event])[0]


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(body: EventIn, db: DB, user: CurrentUser):
    """Everyone's submissions wait for review, except that admins can choose to publish
    now or at a set time (publish / publish_at)."""
    if get_settings().require_verified_email and user.role != Role.admin and not user.email_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Confirm your email address before sending in events. "
                            "Use the link we emailed you, or send a new one from your account page.")
    status_, publish_at = EventStatus.pending, None
    if user.role == Role.admin:
        if body.publish == "now":
            status_ = EventStatus.approved
        elif body.publish == "later":
            if body.publish_at <= utcnow():
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "publish_at must be in the future")
            status_, publish_at = EventStatus.approved, body.publish_at.astimezone(UTC)
    if body.venue_id is not None:
        venue = db.get(Venue, body.venue_id)
        if venue is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "No venue with that venue_id")
    else:
        venue = get_or_create_venue(db, body.venue)
    event = Event(**body.model_dump(exclude={"venue_id", "venue", "publish", "publish_at"}), venue_id=venue.id,
                  submitted_by=user.id, status=status_, publish_at=publish_at)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.patch("/{event_id}", response_model=EventOut)
def update_event(event_id: int, body: EventPatch, db: DB, _: AdminUser):
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    changes = body.model_dump(exclude_unset=True)
    if "venue_id" in changes and db.get(Venue, changes["venue_id"]) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "No venue with that venue_id")
    if changes.get("publish_at") is not None:
        changes["publish_at"] = changes["publish_at"].astimezone(UTC)
    for field, value in changes.items():
        setattr(event, field, value)
    if event.begin_date and event.end_date and event.end_date < event.begin_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "end_date is before begin_date")
    db.commit()
    db.refresh(event)
    return event


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(event_id: int, db: DB, _: AdminUser):
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    db.delete(event)
    db.commit()
