"""Request and response shapes. FastAPI validates against these and builds /docs from them."""
import uuid
from datetime import date, date as date_, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from .models import Area, EventStatus, PlanStatus, Role

Category = Literal["Theater", "Concert", "Comedy", "Visual Art"]
Publish = Literal["now", "later", "review"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth ------------------------------------------------------------------

def _bcrypt_limit(v: str) -> str:
    if len(v.encode()) > 72:  # bcrypt only uses the first 72 bytes
        raise ValueError("Password must be at most 72 bytes")
    return v


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    display_name: str = Field(default="", max_length=80)

    _password_fits_bcrypt = field_validator("password")(_bcrypt_limit)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=8, max_length=72)  # the same rules as at sign-up

    _password_fits_bcrypt = field_validator("new_password")(_bcrypt_limit)


class ProfilePatch(BaseModel):
    """Change your own name and/or email. Leave a field out to keep it.

    Changing the email needs current_password, so someone at an unlocked computer can't take
    over the account by moving it to their own address. The name needs no password; a blank
    name is allowed and means nothing is shown (no "Suggested by" credit either).
    """
    display_name: str | None = Field(default=None, max_length=80)
    email: EmailStr | None = None
    current_password: str | None = Field(default=None, max_length=200)


class UserOut(ORM):
    id: uuid.UUID
    email: str
    email_verified: bool  # they've opened the link emailed to this address
    display_name: str
    role: Role
    dolphins: bool  # CC-61: plans show the dolphin icons for this account
    created_at: datetime


class TokenIn(BaseModel):
    """The token from an emailed link (the part after #verify= or #reset=)."""
    token: str = Field(min_length=10, max_length=200)


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(TokenIn):
    new_password: str = Field(min_length=8, max_length=72)  # the same rules as at sign-up

    _password_fits_bcrypt = field_validator("new_password")(_bcrypt_limit)


class MessageOut(BaseModel):
    detail: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut


# ---- venues ----------------------------------------------------------------

class VenueOut(ORM):
    id: int
    name: str
    neighborhood: str
    area: Area


class VenueIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    neighborhood: str = Field(default="", max_length=120)
    area: Area | None = None  # worked out from the neighborhood when left out


class VenuePatch(BaseModel):
    """Rename a venue or move it (CC-57). Left out = unchanged. A new neighborhood without an area
    gets the area worked out from it."""
    name: str | None = Field(default=None, min_length=1, max_length=200)
    neighborhood: str | None = Field(default=None, max_length=120)
    area: Area | None = None


class VenueAdminOut(VenueOut):
    """A venue with how many events use it, for the admin venues list."""
    events: int
    sheet_events: int  # of those, how many come from the Google Sheet (their venue follows the sheet)


class VenueMergeOut(BaseModel):
    moved: int  # events that moved over
    into: VenueOut


# ---- events ----------------------------------------------------------------

class EventOut(ORM):
    id: int
    source_id: str | None  # id from the old Google Sheet site, so its share links keep working
    title: str
    venue: VenueOut
    begin_date: date | None
    end_date: date | None
    open_run: bool
    cost_text: str
    min_price: float | None  # a JSON number for the page; stored exactly as NUMERIC
    category: str
    url: str
    status: EventStatus
    publish_at: datetime | None  # set while an approved event is scheduled for later
    # "Suggested by …": the submitter's display name if they opted in for this event, else null.
    # Never the email or id; those are only in EventAdminOut.
    submitted_by_name: str | None


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int


class NeighborhoodOut(BaseModel):
    name: str
    area: Area


class EventFacets(BaseModel):
    categories: list[str]  # most events first
    neighborhoods: list[NeighborhoodOut]  # alphabetical
    current: int  # live and not closed yet (as of the caller's date)
    total: int  # live, including closed ones


class SubmitterOut(ORM):
    id: uuid.UUID
    display_name: str
    email: str


class EventAdminOut(EventOut):
    """EventOut plus who sent it in. Only the /admin routes return this."""
    # null for sheet and seed events, and when the submitter's account was deleted
    submitted_by: SubmitterOut | None = Field(validation_alias="submitter")
    credit_me: bool  # whether they asked to be credited publicly


class EventAdminPage(BaseModel):
    items: list[EventAdminOut]
    total: int
    limit: int
    offset: int


class _EventFields(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    begin_date: date | None = None
    end_date: date | None = None
    open_run: bool = False
    cost_text: str = Field(default="", max_length=120)
    min_price: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=2)
    category: Category
    url: str = Field(default="", max_length=2000)

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: str) -> str:
        if v and not v.startswith(("https://", "http://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @model_validator(mode="after")
    def _dates_in_order(self):
        if self.begin_date and self.end_date and self.end_date < self.begin_date:
            raise ValueError("end_date is before begin_date")
        return self


class EventIn(_EventFields):
    """Give venue_id for a known venue, or venue (name + neighborhood) to use or add one.

    publish / publish_at are for admins: "now" goes live at once, "later" goes live at
    publish_at, "review" waits in the review queue. Everyone else's submissions always
    wait for review, whatever these say.
    """
    venue_id: int | None = None
    venue: VenueIn | None = None
    publish: Publish = "review"
    publish_at: datetime | None = None
    # Opt in to a public "Suggested by <display name>" on this event. Who submitted it
    # always comes from the sign-in, never from the request.
    credit_me: bool = False

    @model_validator(mode="after")
    def _one_venue(self):
        if (self.venue_id is None) == (self.venue is None):
            raise ValueError("Give exactly one of venue_id or venue")
        return self

    @model_validator(mode="after")
    def _publish_time(self):
        if self.publish == "later":
            if self.publish_at is None:
                raise ValueError('publish_at is required when publish is "later"')
            if self.publish_at.tzinfo is None:
                raise ValueError("publish_at needs a time zone, e.g. 2026-11-01T09:00:00-05:00")
        elif self.publish_at is not None:
            raise ValueError('publish_at only goes with publish "later"')
        return self


class EventPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    venue_id: int | None = None
    begin_date: date | None = None
    end_date: date | None = None
    open_run: bool | None = None
    cost_text: str | None = Field(default=None, max_length=120)
    min_price: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=2)
    category: Category | None = None
    url: str | None = Field(default=None, max_length=2000)
    status: EventStatus | None = None
    publish_at: datetime | None = None  # null publishes as soon as the event is approved

    @field_validator("publish_at")
    @classmethod
    def _aware(cls, v):
        if v is not None and v.tzinfo is None:
            raise ValueError("publish_at needs a time zone")
        return v

    @field_validator("url")
    @classmethod
    def _http_only(cls, v):  # the same rule as new events (CC-56: the site's edit form sends it)
        if v and not v.startswith(("https://", "http://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @model_validator(mode="after")
    def _no_nulls_where_required(self):
        # Leaving a field out keeps it; only dates, price and publish time can be cleared with null.
        for name in ("title", "venue_id", "open_run", "cost_text", "category", "url", "status"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} can't be empty")
        return self


# ---- sheet sync log --------------------------------------------------------

class SheetSyncOut(ORM):
    id: int
    started_at: datetime
    finished_at: datetime | None
    result: str
    rows_read: int
    added: int
    updated: int
    unchanged: int
    withdrawn: int
    restored: int
    skipped: int
    message: str


# ---- attended --------------------------------------------------------------

class PlanIn(BaseModel):
    """status "interested", or "going" with the date (a day the event is on, not in the past)."""
    status: Literal["interested", "going"]
    date: date_ | None = None

    @model_validator(mode="after")
    def _date_only_when_going(self):
        if self.status == "going" and self.date is None:
            raise ValueError('Choose the date you\'re going ("date")')
        if self.status == "interested" and self.date is not None:
            raise ValueError('A date only goes with "going"')
        return self


class PlanOut(ORM):
    event: EventOut
    status: PlanStatus
    date: date_ | None = Field(validation_alias="going_on")
    updated_at: datetime


class AttendedOut(ORM):
    event: EventOut
    created_at: datetime
