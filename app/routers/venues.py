from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from ..areas import area_of
from ..deps import DB, AdminUser
from ..models import Venue
from ..schemas import VenueIn, VenueOut

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
    venue = Venue(name=name, neighborhood=hood, area=body.area or area_of(hood))
    db.add(venue)
    db.commit()
    return venue
