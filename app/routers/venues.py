from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import delete, func, select, update

from ..addresses import suggestions as address_suggestions
from ..areas import nearby_area, region_of
from ..deps import DB, AdminUser, OptionalUser
from ..models import Event, Role, SubmissionRule, Venue, VenueAddressSuggestion, VenueWebsiteDismissal
from ..schemas import (AddressChoice, AddressPick, AddressSuggestionOut, VenueAdd, VenueAdminOut, VenueIn, VenueMergeOut, VenueOut,
                       VenuePatch, WebsiteChoice, WebsiteSuggestionOut)
from ..visibility import utcnow
from ..websites import suggestions

router = APIRouter(prefix="/venues", tags=["venues"])


@router.get("", response_model=list[VenueOut])
def list_venues(db: DB, user: OptionalUser, q: Annotated[str | None, Query(max_length=100)] = None):
    stmt = select(Venue).order_by(Venue.name, Venue.neighborhood)
    if user is None or user.role != Role.admin:  # CC-188, CC-219: admins pick these venues while reviewing or editing events
        stmt = stmt.where(Venue.pending.is_(False), Venue.hidden.is_(False))
    if q:
        stmt = stmt.where(Venue.name.icontains(q, autoescape=True))
    return db.scalars(stmt).all()


def _add(db: DB, body: VenueIn, **address: str) -> Venue:
    name, hood = body.name.strip(), body.neighborhood.strip()
    exists = db.scalar(select(Venue.id).where(func.lower(Venue.name) == name.lower(),
                                              func.lower(Venue.neighborhood) == hood.lower()))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "That venue already exists")
    area = nearby_area(hood, body.area)
    venue = Venue(name=name, neighborhood=hood, area=area, region=region_of(hood, area), **address)
    db.add(venue)
    db.commit()
    return venue


@router.post("", response_model=VenueOut, status_code=status.HTTP_201_CREATED)
def create_venue(body: VenueAdd, db: DB, _: AdminUser):
    """CC-218: the Add a venue form. The address is required here, and only here."""
    return _add(db, body, address=body.address, city=body.city, zip_code=body.zip_code)


@router.post("/from-event", response_model=VenueOut, status_code=status.HTTP_201_CREATED)
def create_venue_from_event(body: VenueIn, db: DB, _: AdminUser):
    """A new venue typed while editing an event or approving a found one: no address asked for at that point."""
    return _add(db, body)


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
                         standard_min_price=None if venue.standard_min_price is None else float(venue.standard_min_price),
                         category=venue.category, opera=venue.opera, website=venue.website or "",
                         address=venue.address or "", city=venue.city or "", zip_code=venue.zip_code or "",
                         pending=venue.pending, hidden=venue.hidden)


@router.get("/manage", response_model=list[VenueAdminOut])
def manage_list(db: DB, _: AdminUser, q: Annotated[str | None, Query(max_length=100)] = None):
    """Every venue with how many events use it (any status). q matches the name or neighborhood."""
    stmt = select(Venue).order_by(func.lower(Venue.name), func.lower(Venue.neighborhood), Venue.id)
    if q:
        stmt = stmt.where(Venue.name.icontains(q, autoescape=True) | Venue.neighborhood.icontains(q, autoescape=True))
    venues = db.scalars(stmt).all()
    counts = _counts(db)
    return [_admin_out(v, counts) for v in venues]


# ---- websites (admins, CC-213) -----------------------------------------------------------

@router.get("/website-suggestions", response_model=list[WebsiteSuggestionOut])
def website_suggestions(db: DB, _: AdminUser):
    """Suggested websites for venues that have none. Nothing is saved until an admin confirms one."""
    return suggestions(db)


@router.post("/{venue_id}/website", response_model=VenueAdminOut)
def confirm_website(venue_id: int, body: WebsiteChoice, db: DB, _: AdminUser):
    """Confirm a suggestion (as suggested, or edited). Never replaces a website already set: change that on the venue."""
    venue = _venue(db, venue_id)
    if venue.website:
        raise HTTPException(status.HTTP_409_CONFLICT, "This venue already has a website. Edit it on the venue instead.")
    venue.website = body.url
    db.commit()
    return _admin_out(venue, _counts(db, [venue.id]))


@router.post("/{venue_id}/website/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_website(venue_id: int, body: WebsiteChoice, db: DB, _: AdminUser):
    """Don't suggest this address for this venue again."""
    venue = _venue(db, venue_id)
    if not db.get(VenueWebsiteDismissal, (venue.id, body.url)):
        db.add(VenueWebsiteDismissal(venue_id=venue.id, url=body.url))
        db.commit()


# ---- street addresses (admins, CC-218) ---------------------------------------------------

@router.get("/address-suggestions", response_model=list[AddressSuggestionOut])
def list_address_suggestions(db: DB, _: AdminUser):
    """Addresses seen for venues that have none. Nothing reaches a venue until an admin confirms one."""
    return address_suggestions(db)


@router.post("/{venue_id}/address", response_model=VenueAdminOut)
def confirm_address(venue_id: int, body: AddressChoice, db: DB, _: AdminUser):
    """Confirm a suggestion (as suggested, or edited). Never replaces an address already set: change that on the venue."""
    venue = _venue(db, venue_id)
    if venue.address:
        raise HTTPException(status.HTTP_409_CONFLICT, "This venue already has an address. Edit it on the venue instead.")
    venue.address, venue.city, venue.zip_code = body.address, body.city, body.zip_code
    db.commit()
    return _admin_out(venue, _counts(db, [venue.id]))


@router.post("/{venue_id}/address/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_address(venue_id: int, body: AddressPick, db: DB, _: AdminUser):
    """Don't suggest this address for this venue again."""
    venue = _venue(db, venue_id)
    row = db.get(VenueAddressSuggestion, (venue.id, body.address))
    if row is None:
        row = VenueAddressSuggestion(venue_id=venue.id, address=body.address)
        db.add(row)
    row.dismissed_at = row.dismissed_at or utcnow()
    db.commit()


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
        venue.area = nearby_area(hood)
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
    if changes.get("website") is not None:  # CC-213: typed by an admin; "" clears it
        venue.website = changes["website"]
    # CC-218: typed by an admin; all three, or "" for all three to clear it
    parts = {f: changes[f] if changes.get(f) is not None else getattr(venue, f)
             for f in ("address", "city", "zip_code")}
    if any(parts.values()) and not all(parts.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "An address needs the street, city and ZIP together")
    for field, value in parts.items():
        setattr(venue, field, value)
    if changes.get("standard_cost") is not None:  # CC-89: set together; the price goes with the words
        venue.standard_cost = changes["standard_cost"].strip()
        venue.standard_min_price = changes.get("standard_min_price") if venue.standard_cost else None
    # CC-125: the default for new events here; its events keep the category they have. Opera goes with Music only.
    if changes.get("opera"):
        if changes.get("category", "Music") != "Music":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "An opera's category is Music")
        venue.category, venue.opera = "Music", True
    else:
        if "category" in changes:
            venue.category = changes["category"]
        if "opera" in changes or venue.category != "Music":
            venue.opera = False
    db.commit()
    return _admin_out(venue, _counts(db, [venue.id]))


def _set_hidden(db: DB, venue_id: int, hidden: bool) -> VenueAdminOut:
    venue = _venue(db, venue_id)
    venue.hidden = hidden
    db.commit()
    return _admin_out(venue, _counts(db, [venue.id]))


@router.post("/{venue_id}/hide", response_model=VenueAdminOut)
def hide_venue(venue_id: int, db: DB, _: AdminUser):
    """CC-219: out of the public venue list, for a venue that can't be deleted. Its events stay listed."""
    return _set_hidden(db, venue_id, True)


@router.post("/{venue_id}/unhide", response_model=VenueAdminOut)
def unhide_venue(venue_id: int, db: DB, _: AdminUser):
    return _set_hidden(db, venue_id, False)


@router.post("/{venue_id}/merge-into/{other_id}", response_model=VenueMergeOut)
def merge_venue(venue_id: int, other_id: int, db: DB, _: AdminUser):
    """Move every event from this venue to the other one, then remove this one."""
    if venue_id == other_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a different venue to merge into")
    venue, into = _venue(db, venue_id), _venue(db, other_id)
    moved = db.execute(update(Event).where(Event.venue_id == venue.id).values(venue_id=into.id)).rowcount
    # CC-100: an auto-reject rule for this venue now covers the one it's merged into (unless it has one already)
    if db.scalar(select(SubmissionRule.id).where(SubmissionRule.kind == "venue", SubmissionRule.venue_id == into.id)):
        db.execute(delete(SubmissionRule).where(SubmissionRule.venue_id == venue.id))
    else:
        db.execute(update(SubmissionRule).where(SubmissionRule.venue_id == venue.id).values(venue_id=into.id))
    for field in ("open_hours", "show_times"):  # keep the hours if the venue merged into has none
        if not getattr(into, field) and getattr(venue, field):
            setattr(into, field, getattr(venue, field))
            setattr(into, field + "_by", getattr(venue, field + "_by"))
    if not into.standard_cost and venue.standard_cost:  # CC-89: and its standard cost
        into.standard_cost, into.standard_min_price = venue.standard_cost, venue.standard_min_price
    if not into.website and venue.website:  # CC-213: and its website
        into.website = venue.website
    if not into.address and venue.address:  # CC-218: and its address
        into.address, into.city, into.zip_code = venue.address, venue.city, venue.zip_code
    into.no_source = into.no_source or venue.no_source  # CC-221: a deleted source stays deleted
    into.pending = into.pending and venue.pending  # CC-188: a live venue's events can't end up at a hidden one
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
