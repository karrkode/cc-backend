from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select, update

from ..areas import area_of, region_of
from ..deps import DB, AdminUser
from ..models import Event, Venue
from ..schemas import VenueAdminOut, VenueIn, VenueMergeOut, VenueOut, VenuePatch

router = APIRouter(prefix="/venues", tags=["venues"])


@router.get("", response_model=list[VenueOut])
def list_venues(db: DB, q: Annotated[str | None, Query(max_length=100)] = None):
    stmt = select(Venue).order_by(Venue.name, Venue.neighborhood)
    if q:
        stmt = stmt.where(Venue.name.icontains(q, autoescape=True))
    return db.scalars(stmt).all()


@router.post("", response_model=VenueOut, status_code=status.HTTP_201_CREATED)
def create_venue(body: VenueIn, db: DB, _: AdminUser):
    name, hood = body.name.strip(), body.neighborhood.strip()
    exists = db.scalar(select(Venue.id).where(func.lower(Venue.name) == name.lower(),
                                              func.lower(Venue.neighborhood) == hood.lower()))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "That venue already exists")
    area = body.area or area_of(hood)
    venue = Venue(name=name, neighborhood=hood, area=area, region=region_of(hood, area))
    db.add(venue)
    db.commit()
    return venue


# ---- managing venues (admins, CC-57) ---------------------------------------------------

def _venue(db: DB, venue_id: int) -> Venue:
    venue = db.get(Venue, venue_id)
    if venue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Venue not found")
    return venue


def _counts(db: DB, ids: list[int] | None = None) -> dict[int, int]:
    stmt = select(Event.venue_id, func.count(Event.id)).group_by(Event.venue_id)
    if ids is not None:
        stmt = stmt.where(Event.venue_id.in_(ids))
    return dict(db.execute(stmt).all())


def _admin_out(venue: Venue, counts: dict[int, int]) -> VenueAdminOut:
    return VenueAdminOut(id=venue.id, name=venue.name, neighborhood=venue.neighborhood, area=venue.area,
                         region=venue.region, events=counts.get(venue.id, 0), open_hours=venue.open_hours or {},
                         open_hours_by=venue.open_hours_by or "", show_times=venue.show_times or {},
                         show_times_by=venue.show_times_by or "", standard_cost=venue.standard_cost or "",
                         standard_min_price=None if venue.standard_min_price is None else float(venue.standard_min_price))


@router.get("/manage", response_model=list[VenueAdminOut])
def manage_list(db: DB, _: AdminUser, q: Annotated[str | None, Query(max_length=100)] = None):
    """Every venue with how many events use it (any status). q matches the name or neighborhood."""
    stmt = select(Venue).order_by(func.lower(Venue.name), func.lower(Venue.neighborhood), Venue.id)
    if q:
        stmt = stmt.where(Venue.name.icontains(q, autoescape=True) | Venue.neighborhood.icontains(q, autoescape=True))
    venues = db.scalars(stmt).all()
    counts = _counts(db)
    return [_admin_out(v, counts) for v in venues]


@router.patch("/{venue_id}", response_model=VenueAdminOut)
def update_venue(venue_id: int, body: VenuePatch, db: DB, _: AdminUser):
    venue = _venue(db, venue_id)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "name can't be empty")
    name = (changes.get("name") or venue.name).strip()
    hood = (changes["neighborhood"] if changes.get("neighborhood") is not None else venue.neighborhood).strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "name can't be empty")
    clash = db.scalar(select(Venue.id).where(func.lower(Venue.name) == name.lower(),
                                             func.lower(Venue.neighborhood) == hood.lower(), Venue.id != venue.id))
    if clash:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Another venue already has that name and neighborhood. Merge this one into it instead.")
    if hood != venue.neighborhood and changes.get("area") is None:
        venue.area = area_of(hood)
    if changes.get("area") is not None:
        venue.area = changes["area"]
    if "region" in changes:  # set (or cleared) by an admin
        venue.region = changes["region"]
    elif hood != venue.neighborhood or "area" in changes:  # moved: work the side of town out again
        venue.region = region_of(hood, venue.area)
    venue.name, venue.neighborhood = name, hood
    for field in ("open_hours", "show_times"):  # CC-78: an admin's hours stay; clearing lets discovery fill them again
        if changes.get(field) is not None:
            value = {day: (list(v) if isinstance(v, tuple) else v) for day, v in changes[field].items()}
            setattr(venue, field, value)
            setattr(venue, field + "_by", "admin" if value else "")
    if changes.get("standard_cost") is not None:  # CC-89: set together; the price goes with the words
        venue.standard_cost = changes["standard_cost"].strip()
        venue.standard_min_price = changes.get("standard_min_price") if venue.standard_cost else None
    db.commit()
    return _admin_out(venue, _counts(db, [venue.id]))


@router.post("/{venue_id}/merge-into/{other_id}", response_model=VenueMergeOut)
def merge_venue(venue_id: int, other_id: int, db: DB, _: AdminUser):
    """Move every event from this venue to the other one, then remove this one."""
    if venue_id == other_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a different venue to merge into")
    venue, into = _venue(db, venue_id), _venue(db, other_id)
    moved = db.execute(update(Event).where(Event.venue_id == venue.id).values(venue_id=into.id)).rowcount
    for field in ("open_hours", "show_times"):  # keep the hours if the venue merged into has none
        if not getattr(into, field) and getattr(venue, field):
            setattr(into, field, getattr(venue, field))
            setattr(into, field + "_by", getattr(venue, field + "_by"))
    if not into.standard_cost and venue.standard_cost:  # CC-89: and its standard cost
        into.standard_cost, into.standard_min_price = venue.standard_cost, venue.standard_min_price
    db.delete(venue)
    db.commit()
    return VenueMergeOut(moved=moved, into=into)


@router.delete("/{venue_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_venue(venue_id: int, db: DB, _: AdminUser):
    """Only a venue no event uses (pending, withdrawn and rejected ones count too)."""
    venue = _venue(db, venue_id)
    n = db.scalar(select(func.count(Event.id)).where(Event.venue_id == venue.id))
    if n:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{n} event{'s' if n != 1 else ''} still use this venue. "
                            "Merge it into another venue instead, or move those events first.")
    db.delete(venue)
    db.commit()
