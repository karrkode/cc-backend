from datetime import UTC, date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, case, func, or_, select, true

from ..areas import area_of, region_of
from ..categories import CATEGORIES, OPERA_IS, category_condition
from ..config import get_settings
from ..deps import DB, AdminUser, CurrentUser, OptionalUser
from .. import photos
from ..autoreject import reject_if_caught
from .. import fromlink
from ..models import Area, Event, EventRunDate, EventStatus, Plan, PlanStatus, Region, Role, Venue, WentPhoto
from ..crewplans import attach_crew_plans, crew_plans_condition
from ..picks import attach_picks, pick_condition
from ..schemas import EventFacets, EventIn, EventOut, EventPage, EventPatch, LinkDraft, LinkIn, RunDate, VenueIn
from ..stats import today_in_chicago
from ..visibility import live_condition, utcnow, visible_to

router = APIRouter(prefix="/events", tags=["events"])


def get_or_create_venue(db: DB, v: VenueIn) -> Venue:
    name, hood = v.name.strip(), v.neighborhood.strip()
    venue = db.scalar(select(Venue).where(func.lower(Venue.name) == name.lower(),
                                          func.lower(Venue.neighborhood) == hood.lower()))
    if venue is None:
        area = v.area or area_of(hood)
        venue = Venue(name=name, neighborhood=hood, area=area, region=region_of(hood, area))
        db.add(venue)
        db.flush()
    return venue


def _no_past_run_dates(dates: list[RunDate], kept: set[date] = frozenset()) -> None:
    """CC-96: a fixed run's dates can't be in the past (in Chicago), except ones the event already had."""
    today = today_in_chicago()
    if any(r.date < today and r.date not in kept for r in dates):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A fixed run's dates can't be in the past")


def _run_dates_of(dates: list[RunDate]) -> list[EventRunDate]:
    return [EventRunDate(date=r.date, time=r.time, end_time=r.end_time) for r in dates]


def _on_between(date_from: date | None, date_to: date | None):
    """CC-96: an event with a fixed run is only on its chosen dates, not the days between them."""
    window = and_(EventRunDate.date >= date_from if date_from else true(), EventRunDate.date <= date_to if date_to else true())
    return or_(~Event.run_dates.any(), Event.run_dates.any(window))


Sort = Literal["start", "closing", "opening", "az", "price"]


def price_or_usual():
    """CC-89: the event's own lowest price, else its venue's standard one (the query joins Venue)."""
    return func.coalesce(Event.min_price, Venue.standard_min_price)


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
    if sort == "price":  # cheapest first, the venue's standard cost standing in (CC-89); "price not posted" last
        return (price_or_usual().asc().nulls_last(), *closing)
    return (Event.begin_date.asc().nulls_first(), Event.title, Event.id)  # start: TBD first, as the old site


@router.get("", response_model=EventPage)
def list_events(
    db: DB,
    q: Annotated[str | None, Query(max_length=100, description="Search titles, venues and neighborhoods")] = None,
    area: Annotated[list[Area] | None, Query()] = None,
    region: Annotated[list[Region] | None, Query(description="Only these sides of town (CC-73)")] = None,
    hood: Annotated[list[str] | None, Query(description="Only these neighborhoods (exact names)")] = None,
    category: Annotated[list[str] | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from", description="Running on or after this date")] = None,
    date_to: Annotated[date | None, Query(alias="to", description="Running on or before this date")] = None,
    ended_before: Annotated[date | None, Query(description="Only events that closed before this date")] = None,
    max_price: Annotated[Decimal | None, Query(ge=0, description="0 = free only")] = None,
    include_past: bool = False,
    mine: Annotated[list[PlanStatus] | None, Query(description="Only events you're interested in or going to (needs sign-in)")] = None,
    picks: Annotated[list[PlanStatus] | None, Query(description="Only the owner's picks of these kinds (the dolphins)")] = None,
    crews: Annotated[str | None, Query(pattern=r"^(all|\d+)$", description='Only events your crews have shared plans or "I\'ve been" marks for: "all", or a crew id (needs sign-in)')] = None,
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
    picks (CC-66): only the owner's public picks of those kinds (the dolphin filter buttons).
    crews (CC-75): only events other members of your crews (all, or one) have shared plans for, or (CC-94)
    have been to."""
    conds = [live_condition()]
    if mine:
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to filter by your plans")
        conds.append(Event.id.in_(select(Plan.event_id).where(Plan.user_id == user.id, Plan.status.in_(mine))))
    if picks:
        conds.append(pick_condition(picks))
    if crews:
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to filter by your crews' plans")
        conds.append(crew_plans_condition(db, user, None if crews == "all" else int(crews)))
    if q:
        conds.append(or_(Event.title.icontains(q, autoescape=True), Venue.name.icontains(q, autoescape=True),
                         Venue.neighborhood.icontains(q, autoescape=True)))
    if area:
        conds.append(Venue.area.in_(area))
    # CC-85: several places at once. Sides of town and neighborhoods together mean any of them
    # ("Northside" and "Pilsen": everything on the Northside, plus Pilsen).
    if region and hood:
        conds.append(or_(Venue.region.in_(region), Venue.neighborhood.in_(hood)))
    elif region:
        conds.append(Venue.region.in_(region))
    elif hood:
        conds.append(Venue.neighborhood.in_(hood))
    if category:  # CC-125: an opera is also under Theater
        conds.append(category_condition(category))
    if ended_before:
        conds.append(Event.end_date < ended_before)
    elif not include_past:
        conds.append(or_(Event.end_date.is_(None), Event.end_date >= date.today()))
    if date_from:  # still running on date_from
        conds.append(or_(Event.end_date.is_(None), Event.end_date >= date_from))
    if date_to:  # already started by date_to
        conds.append(or_(Event.begin_date.is_(None), Event.begin_date <= date_to))
    if date_from or date_to:
        conds.append(_on_between(date_from, date_to))
    if max_price is not None:
        conds.append(price_or_usual() <= max_price)  # "price not posted" anywhere (NULL) drops out

    total = db.scalar(select(func.count(Event.id)).join(Venue).where(*conds))
    rows = db.scalars(select(Event).join(Venue).where(*conds)
                      .order_by(*_order(sort, date_from or date.today()))
                      .limit(limit).offset(offset)).all()
    return EventPage(items=attach_crew_plans(db, attach_picks(db, rows), user), total=total, limit=limit, offset=offset)


@router.get("/facets", response_model=EventFacets)
def facets(db: DB, today: Annotated[date | None, Query(description="The caller's local date")] = None):
    """What the site needs to build its filters and headline counts, without loading every event:
    the categories (CC-125: always the five) and neighborhoods in use, and how many events there are."""
    today = today or date.today()
    live = live_condition()
    hoods = db.execute(select(Venue.neighborhood, Venue.area, Venue.region).join(Event).where(live, Venue.neighborhood != "")
                       .group_by(Venue.neighborhood, Venue.area, Venue.region).order_by(Venue.neighborhood)).all()
    current = db.scalar(select(func.count(Event.id)).where(live, or_(Event.end_date.is_(None), Event.end_date >= today)))
    return EventFacets(categories=list(CATEGORIES), neighborhoods=[{"name": n, "area": a, "region": r} for n, a, r in hoods],
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
    return attach_crew_plans(db, attach_picks(db, [event]), user)[0]


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(body: EventIn, db: DB, user: CurrentUser):
    """Everyone's submissions wait for review, except that admins can choose to publish
    now or at a set time (publish / publish_at)."""
    if get_settings().require_verified_email and user.role != Role.admin and not user.email_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Confirm your email address before sending in events. "
                            "Use the link we emailed you, or send a new one from your account page.")
    _no_past_run_dates(body.run_dates)
    event = new_event(db, body, user)
    if user.role != Role.admin:
        reject_if_caught(db, event)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.post("/from-link", response_model=LinkDraft)
def from_link(body: LinkIn, db: DB, user: CurrentUser):
    """CC-124: read an event's page and send back a draft for the Suggest form. Saves no event. 422 when the
    link isn't a public web page or the page can't be read, with a message to show."""
    key = str(user.id)
    if not fromlink.minute_limiter.hit(key, fromlink.PER_MINUTE):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of links at once. Wait a minute and try again.")
    if not fromlink.day_limiter.hit(key, fromlink.PER_DAY):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's today's links. Enter the event yourself, or try again tomorrow.")
    url = body.url.strip()
    try:
        return fromlink.read_link(db, url, today_in_chicago())
    except fromlink.PrivateAddress as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"We can't read that link: {e}.") from e
    except fromlink.Unreadable as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, fromlink.WHY[e.why]) from e


def new_event(db: DB, body: EventIn, user) -> Event:
    """The event a submission makes (not yet added): admins choose when it goes live; everyone else's
    waits for review. Also used to approve a found event (CC-71)."""
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
    category, opera = category_for(body, venue, user)
    return Event(**body.model_dump(exclude={"venue_id", "venue", "publish", "publish_at", "run_dates", "category", "opera"}),
                 category=category, opera=opera, venue_id=venue.id, submitted_by=user.id, status=status_,
                 publish_at=publish_at, run_dates=_run_dates_of(body.run_dates))


def category_for(body: EventIn, venue: Venue, user) -> tuple[str, bool]:
    """CC-125: the new event's category and opera flag. Left out, the venue's default; an event can differ from its
    venue (an Educational talk at a Visual Art venue). Only admins mark opera; left out, a Music event at an opera
    venue is one. A venue with no default yet takes this event's."""
    if body.opera and user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can mark an event as opera")
    category = body.category or venue.category
    if category is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a type")
    opera = body.opera if body.opera is not None else venue.opera and category == OPERA_IS
    if venue.category is None:
        venue.category, venue.opera = category, opera
    return category, opera


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
    if changes.get("opera"):  # CC-125: an opera is Music; any other category isn't an opera
        if changes.get("category", OPERA_IS) != OPERA_IS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "An opera's category is Music")
        changes["category"] = OPERA_IS
    elif changes.get("category") not in (None, OPERA_IS):
        changes["opera"] = False
    if "run_dates" in changes:  # CC-96: the run sets the dates; new dates can't be in the past (ones it had can stay)
        _no_past_run_dates(body.run_dates, {r.date for r in event.run_dates})
        event.run_dates.clear()
        db.flush()  # the old dates go first, so a date kept in the new run doesn't clash with itself
        event.run_dates = _run_dates_of(body.run_dates)
        del changes["run_dates"]
        if body.run_dates:
            changes.update(begin_date=body.run_dates[0].date, end_date=body.run_dates[-1].date, open_run=False, weekly={}, weeks={})
    for field, value in changes.items():
        setattr(event, field, value)
    if "weekly" in changes or "weeks" in changes:  # CC-88: weeks of the month only for days it's still on
        event.weeks = {d: sorted(set(w)) for d, w in (event.weeks or {}).items() if d in (event.weekly or {})}
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
    photos.remove(db, db.scalars(select(WentPhoto).where(WentPhoto.event_id == event_id)))  # CC-76: from storage too
    db.delete(event)
    db.commit()
