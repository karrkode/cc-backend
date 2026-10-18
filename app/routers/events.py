from datetime import UTC, date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from ..areas import area_of
from ..config import get_settings
from ..deps import DB, AdminUser, CurrentUser, OptionalUser
from ..models import Area, Event, EventStatus, Role, Venue
from ..schemas import EventIn, EventOut, EventPage, EventPatch, VenueIn
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


@router.get("", response_model=EventPage)
def list_events(
    db: DB,
    q: Annotated[str | None, Query(max_length=100, description="Search titles and venue names")] = None,
    area: Annotated[list[Area] | None, Query()] = None,
    category: Annotated[list[str] | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from", description="Running on or after this date")] = None,
    date_to: Annotated[date | None, Query(alias="to", description="Running on or before this date")] = None,
    max_price: Annotated[Decimal | None, Query(ge=0, description="0 = free only")] = None,
    include_past: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Live events (approved, and past any scheduled publish time), soonest first.
    Events with no start date (TBD) come first, as on the site."""
    conds = [live_condition()]
    if q:
        conds.append(or_(Event.title.icontains(q, autoescape=True), Venue.name.icontains(q, autoescape=True)))
    if area:
        conds.append(Venue.area.in_(area))
    if category:
        conds.append(Event.category.in_(category))
    if not include_past:
        conds.append(or_(Event.end_date.is_(None), Event.end_date >= date.today()))
    if date_from:  # still running on date_from
        conds.append(or_(Event.end_date.is_(None), Event.end_date >= date_from))
    if date_to:  # already started by date_to
        conds.append(or_(Event.begin_date.is_(None), Event.begin_date <= date_to))
    if max_price is not None:
        conds.append(Event.min_price <= max_price)  # "price not posted" (NULL) drops out

    total = db.scalar(select(func.count(Event.id)).join(Venue).where(*conds))
    rows = db.scalars(select(Event).join(Venue).where(*conds)
                      .order_by(Event.begin_date.asc().nulls_first(), Event.title, Event.id)
                      .limit(limit).offset(offset)).all()
    return EventPage(items=rows, total=total, limit=limit, offset=offset)


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: int, db: DB, user: OptionalUser):
    event = db.get(Event, event_id)
    if event is None or not visible_to(event, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return event


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
