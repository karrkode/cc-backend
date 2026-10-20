"""Request and response shapes. FastAPI validates against these and builds /docs from them."""
import uuid
from datetime import date, date as date_, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from .models import Area, EventStatus, PlanStatus, Region, Role

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
    region: Region | None  # CC-73: side of town; null if the neighborhood isn't placed yet


class VenueIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    neighborhood: str = Field(default="", max_length=120)
    area: Area | None = None  # worked out from the neighborhood when left out


# CC-78: a venue's opening hours and usual show times. Times are 24-hour HH:MM; a closing time earlier
# than the opening time runs past midnight (a club open 8 PM to 1 AM).
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Clock = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
OpenHours = dict[Weekday, tuple[Clock, Clock] | None]  # null: closed that day; a day left out isn't known
ShowTimes = dict[Weekday, Annotated[list[Clock], Field(max_length=6)]]


class VenuePatch(BaseModel):
    """Rename a venue or move it (CC-57). Left out = unchanged. A new neighborhood without an area
    gets the area worked out from it."""
    name: str | None = Field(default=None, min_length=1, max_length=200)
    neighborhood: str | None = Field(default=None, max_length=120)
    area: Area | None = None
    region: Region | None = None  # CC-73: send null to clear; left out, it's worked out again when the venue moves
    # CC-78: set by an admin, discovery leaves them alone from then on; {} clears them (discovery may fill them again)
    open_hours: OpenHours | None = None
    show_times: ShowTimes | None = None


class VenueAdminOut(VenueOut):
    """A venue with how many events use it, for the admin venues list, and its hours (CC-78)."""
    events: int
    open_hours: dict = {}
    open_hours_by: str = ""  # "" | discovery | admin
    show_times: dict = {}
    show_times_by: str = ""


class VenueMergeOut(BaseModel):
    moved: int  # events that moved over
    into: VenueOut


# ---- events ----------------------------------------------------------------

class PickOut(BaseModel):
    """CC-66: the owner's public pick on an event (the dolphins on its card)."""
    status: PlanStatus
    date: date_ | None  # set for "going": the day they're going
    by: str  # the account's display name ("" if it has none)


class ScheduleOut(BaseModel):
    """CC-79: when an event is on, for Plan an outing. dates: upcoming specific dates and times
    ({"date", "time"}; time may be null). weekly: usual start times by weekday, the event's own
    (weekly_from "event") or its venue's ("venue"). hours: the venue's opening hours."""
    dates: list[dict] = []
    weekly: dict = {}
    weekly_from: str = ""
    hours: dict = {}


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
    pick: PickOut | None = None  # CC-66: the owner's public pick (null when there isn't one)
    schedule: ScheduleOut = ScheduleOut()  # CC-79: for the planner; not shown on cards


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int


class NeighborhoodOut(BaseModel):
    name: str
    area: Area
    region: Region | None  # CC-73


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
    weekly: ShowTimes | None = None  # CC-79: its usual show times, over the venue's; {} goes back to the venue's

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
        for name in ("title", "venue_id", "open_run", "cost_text", "category", "url", "status", "weekly"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} can't be empty")
        return self


# ---- event discovery (CC-70) -------------------------------------------------

class DiscoverySourceOut(ORM):
    id: int
    name: str
    kind: str
    pages: list[str]
    reader: str  # claude | aic_api
    venue_id: int | None
    venue_name: str
    every_days: int
    active: bool
    manual: bool = False  # CC-81: checked by hand only, never automatically
    next_check_on: date_ | None  # due on or after this day (null: at the next hourly run)
    unchanged_streak: int
    last_checked_at: datetime | None
    last_changed_at: datetime | None
    last_status: str  # ok | unchanged | blocked | error ("" = never checked)
    last_error: str
    waiting: int = 0  # found items waiting for review
    checks_30d: int = 0
    pages_read_30d: int = 0


class DiscoverySourcePatch(BaseModel):
    every_days: int | None = Field(default=None, ge=1, le=120)
    active: bool | None = None
    manual: bool | None = None  # CC-81: true = check by hand only
    pages: list[str] | None = Field(default=None, min_length=1, max_length=5)

    @field_validator("pages")
    @classmethod
    def _http_only(cls, v):
        if v is not None and any(not p.startswith(("https://", "http://")) or len(p) > 500 for p in v):
            raise ValueError("Each page must be a web address starting with https://")
        return v


class DiscoveryOverview(BaseModel):
    sources: list[DiscoverySourceOut]
    tokens_in_30d: int
    tokens_out_30d: int
    cost_30d_usd: float  # at Claude Haiku 5.5's list price; the Anthropic Console has the exact bill
    # CC-80: this calendar month (Chicago) against the monthly budget; state ok | saving (80%+: event pages
    # wait) | paused (100%: only Check now runs)
    spent_month_usd: float = 0
    budget_usd: float = 10
    budget_state: str = "ok"


class BudgetIn(BaseModel):
    usd: float = Field(ge=1, le=100)  # dollars a month for reading pages with Claude


# ---- admin stats (CC-67) ---------------------------------------------------

class StatsWeek(BaseModel):
    start: date_  # the Monday the week starts (Chicago time)
    registrations: int
    confirmed: int  # of those registrations, how many have confirmed their email
    active: int  # members who used the site while signed in that week
    plans_interested: int
    plans_going: int
    attended: int  # "I've been" marks
    submitted: int  # events members sent in


class StatsRetention(BaseModel):
    eligible: int  # members who joined at least 4 weeks ago (since tracking began)
    retained: int  # of those, how many used the site again 4 or more weeks after joining


class EventInterestOut(BaseModel):
    """CC-69: one event's counts on the admin Stats page."""
    event: EventOut
    interested: int
    going: int
    attended: int


class TrafficDay(BaseModel):
    day: date_
    views: int
    visitors: int  # unique that day
    returning: int  # of those, first seen on an earlier day
    tickets: int  # clicks out to an event's own page
    outings: int  # outings planned


class TrafficOut(BaseModel):
    """CC-68: cookie-free traffic for the admin Stats page. Counts only."""
    days: list[TrafficDay]  # oldest first; the last is today
    by_view: dict[str, int]  # views of each part of the site over the period, most first
    visitors: int  # unique visitors over the whole period
    returning_visitors: int  # of those, how many had visited before
    counting_since: date_ | None


class StatsOut(BaseModel):
    today: date_
    weeks: list[StatsWeek]  # oldest first; the last is the current week so far
    members: int  # every member account (admins left out)
    confirmed_members: int
    active_since: date_ | None  # the first day activity was recorded
    retention: StatsRetention


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


class FoundChange(BaseModel):
    field: str  # cost_text | min_price | begin_date | end_date
    listed: str | float | None  # what the catalog says
    found: str | float | None  # what the source says now


class EventRef(BaseModel):
    id: int
    title: str


class DiscoveredItemOut(ORM):
    """A found event for review (CC-71). venue_id: the venue it would be filed under (null: pick or
    add one); possible_duplicate: a catalog event at that venue with a similar title."""
    id: int
    source_id: int
    source_name: str = ""
    status: str  # new | update | listed | approved | rejected | skipped
    skip_reason: str
    title: str
    url: str
    kind: str
    category: str | None
    venue_name: str
    venue_id: int | None = None
    neighborhood: str = ""
    begin_date: date | None
    end_date: date | None
    open_run: bool
    cost_text: str
    min_price: Decimal | None
    dates_listed: int = 0  # how many performance dates and times the source listed
    proposed: list[FoundChange] = []  # for "update": what would change
    event: EventRef | None = None  # the catalog event, once listed or approved
    possible_duplicate: EventRef | None = None
    first_seen_at: datetime


class UnreadableSource(BaseModel):
    id: int
    name: str
    status: str  # blocked | error
    reason: str
    last_checked_at: datetime | None


class DiscoveredPage(BaseModel):
    items: list[DiscoveredItemOut]
    total: int
    counts: dict[str, int]  # items per status, for the filter
    unreadable: list[UnreadableSource]


class SkipSimilar(BaseModel):
    phrase: str = Field(min_length=2, max_length=200)
    everywhere: bool = False  # all sources, or just this item's


class RejectIn(BaseModel):
    skip_similar: SkipSimilar | None = None


class RejectOut(BaseModel):
    item: DiscoveredItemOut
    also_rejected: int = 0  # other waiting items the new rule caught


class SkipRuleOut(BaseModel):
    id: int
    phrase: str
    source_id: int | None
    source_name: str | None  # None: all sources
    caught: int
    created_at: datetime

