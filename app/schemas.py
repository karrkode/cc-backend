"""Request and response shapes. FastAPI validates against these and builds /docs from them."""
import uuid
from datetime import date, date as date_, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from .models import Area, EventStatus, PlanStatus, Region, Role

# CC-125: the five categories (app.categories.CATEGORIES); Concert became Music
Category = Literal["Theater", "Comedy", "Music", "Visual Art", "Educational"]
Publish = Literal["now", "later", "review"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth ------------------------------------------------------------------

def _bcrypt_limit(v: str) -> str:
    if len(v.encode()) > 72:  # bcrypt only uses the first 72 bytes
        raise ValueError("Password must be at most 72 bytes")
    return v


class JoinSourceIn(BaseModel):
    """CC-121: where a visit that came by the join page came from (CC-110's tags or the referring domain).
    Cleaned by traffic.source_of; only counted, never kept with the account."""
    utm_source: str | None = Field(default=None, max_length=300)
    utm_medium: str | None = Field(default=None, max_length=300)
    utm_campaign: str | None = Field(default=None, max_length=300)
    ref: str | None = Field(default=None, max_length=253)


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    display_name: str = Field(default="", max_length=80)
    join_source: JoinSourceIn | None = None  # CC-121: sent only for a sign-up in a visit that came by /join

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
    can_approve: bool = False  # CC-91: approves "I've been" photos (approvers, and admins)
    approver_offered: bool = False  # CC-91: an admin has offered them the approver permission; they haven't answered
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
    standard_cost: str = ""  # CC-89: what it usually costs, for its events that don't post a price ("" = none)
    standard_min_price: float | None = None  # its lowest price in dollars (0 = free)
    category: str | None = None  # CC-125: the category new events here start with (null: none set)
    opera: bool = False  # and whether they start as opera


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
# CC-88: show days on some weeks of the month only: 1-4 the first to fourth, 5 the last; a day left out is every week
Weeks = dict[Weekday, Annotated[list[Annotated[int, Field(ge=1, le=5)]], Field(min_length=1, max_length=5)]]


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
    # CC-89: what it usually costs, as written, with its lowest price (0 = free); "" clears both
    standard_cost: str | None = Field(default=None, max_length=120)
    standard_min_price: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=2)
    # CC-125: the default category for new events here (null clears it); opera sets it to Music. Its events keep theirs.
    category: Category | None = None
    opera: bool | None = None


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
    weeks: dict = {}  # CC-88: days of the weekly pattern on some weeks of the month only, {"thu": [1, 3]}
    hours: dict = {}


class CrewPlansOut(BaseModel):
    """CC-75: on an event, who from one of your crews is going or interested (by name; never you)."""
    crew_id: int
    crew_name: str
    going: list[str] = []
    interested: list[str] = []
    been: list[str] = []  # CC-117: crew-mates who've marked "I've been" and shared it with this crew


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
    opera: bool = False  # CC-125: an opera (category Music), also listed under Theater and labelled "Opera"
    url: str
    status: EventStatus
    publish_at: datetime | None  # set while an approved event is scheduled for later
    # "Suggested by …": the submitter's display name if they opted in for this event, else null.
    # Never the email or id; those are only in EventAdminOut.
    submitted_by_name: str | None
    pick: PickOut | None = None  # CC-66: the owner's public pick (null when there isn't one)
    schedule: ScheduleOut = ScheduleOut()  # CC-79: for the planner; not shown on cards
    run_dates: list[RunDate] = []  # CC-96: a fixed run's dates, every one (the site shows the upcoming ones)
    crew_plans: list[CrewPlansOut] = []  # CC-75: signed in only: your crew-mates' shared plans


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int


class NeighborhoodOut(BaseModel):
    name: str
    area: Area
    region: Region | None  # CC-73


class LinkIn(BaseModel):
    """CC-124: a link to an event's page, for a draft of the Suggest form."""
    url: Annotated[str, Field(min_length=8, max_length=2000)]


class LinkRunDate(BaseModel):
    date: str
    time: str


class LinkDraft(BaseModel):
    """CC-124: what the page says, for the member to check and send. missing names what it didn't say: title,
    category, venue, neighborhood, dates, price. read_by is "page" (its event data) or "claude"."""
    url: str
    title: str
    category: str | None = None
    venue: str = ""
    neighborhood: str = ""
    begin_date: str | None = None
    end_date: str | None = None
    open_run: bool = False
    cost_text: str = ""
    min_price: float | None = None
    run_dates: list[LinkRunDate] = []
    weekly: dict[str, list[str]] = {}
    weeks: dict[str, list[int]] = {}
    read_by: Literal["page", "claude"]
    other_events: int = 0
    missing: list[str] = []


class EventFacets(BaseModel):
    categories: list[str]  # CC-125: the five, in the site's order
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
    reject_reason: str = ""  # CC-100: set when an auto-reject rule caught it
    created_at: datetime  # when it was sent in


class EventAdminPage(BaseModel):
    items: list[EventAdminOut]
    total: int
    limit: int
    offset: int


class RunDate(BaseModel):
    """CC-96: one date of a fixed run: its start time, and its end time if known."""
    model_config = ConfigDict(from_attributes=True)
    date: date_
    time: Clock
    end_time: Clock | Literal[""] = ""


RUN_DATES_MAX = 30  # CC-96: the product owner's limit; beyond that a weekly schedule fits better
RunDates = Annotated[list[RunDate], Field(max_length=RUN_DATES_MAX)]


def _one_time_a_day(v: list[RunDate]) -> list[RunDate]:
    days = [r.date for r in v]
    if len(set(days)) != len(days):
        raise ValueError("Each date can only be chosen once")
    return sorted(v, key=lambda r: r.date)


class _EventFields(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    begin_date: date | None = None
    end_date: date | None = None
    open_run: bool = False
    cost_text: str = Field(default="", max_length=120)
    min_price: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=2)
    # CC-125: left out, the venue's default category (and opera) is used
    category: Category | None = None
    # CC-125: admins only. Left out, an event at an opera venue whose category is Music is an opera.
    opera: bool | None = None
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

    @model_validator(mode="after")
    def _opera_is_music(self):
        if self.opera and self.category not in (None, "Music"):
            raise ValueError("An opera's category is Music")
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
    weekly: ShowTimes = {}  # CC-88: the days it's on, with start times, for performances; {} = the venue's usual times
    weeks: Weeks = {}  # CC-88: of those days, any on some weeks of the month only
    # CC-96: a fixed run: on these dates only, a time each. Its opening and closing dates are then the first and last
    # of them (begin_date and end_date are set from them), and it has no weekly pattern.
    run_dates: RunDates = []

    _run_dates_once = field_validator("run_dates")(_one_time_a_day)

    @model_validator(mode="after")
    def _fixed_run(self):
        if self.run_dates:
            self.begin_date, self.end_date = self.run_dates[0].date, self.run_dates[-1].date
            self.open_run, self.weekly, self.weeks = False, {}, {}
        return self

    @model_validator(mode="after")
    def _weeks_of_show_days(self):
        self.weeks = {d: sorted(set(w)) for d, w in self.weeks.items() if d in self.weekly}
        return self

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
    opera: bool | None = None  # CC-125: true sets the category to Music; a category other than Music clears it
    url: str | None = Field(default=None, max_length=2000)
    status: EventStatus | None = None
    publish_at: datetime | None = None  # null publishes as soon as the event is approved
    weekly: ShowTimes | None = None  # CC-79: its usual show times, over the venue's; {} goes back to the venue's
    weeks: Weeks | None = None  # CC-88: days of those on some weeks of the month only; {} = every week
    run_dates: RunDates | None = None  # CC-96: a fixed run's dates (sets the opening and closing dates); [] ends the fixed run

    _run_dates_once = field_validator("run_dates")(lambda v: v if v is None else _one_time_a_day(v))

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
        for name in ("title", "venue_id", "open_run", "cost_text", "category", "opera", "url", "status", "weekly", "weeks", "run_dates"):
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


class TrafficSource(BaseModel):
    """CC-110: a tagged link's utm values, or a referring site's domain (medium "site")."""
    source: str
    medium: str
    campaign: str
    visitors: int  # unique over the period
    tickets: int  # clicks out to an event's own page during visits from this source


class TrafficDirect(BaseModel):
    visitors: int  # came with no tags and no referring site (typed it, a bookmark, an app that hides it)
    tickets: int


class JoinSource(BaseModel):
    """CC-121: join page visits and sign-ups from one source."""
    source: str
    medium: str
    campaign: str
    visits: int  # a visitor on a day
    signups: int  # sign-ups in visits that came by the join page
    rate: float | None  # signups / visits; None with no visits


class JoinDirect(BaseModel):
    visits: int
    signups: int
    rate: float | None


class JoinStats(BaseModel):
    visits: int
    signups: int
    rate: float | None
    sources: list[JoinSource] = []  # most visits first
    direct: JoinDirect = JoinDirect(visits=0, signups=0, rate=None)


class TrafficOut(BaseModel):
    """CC-68: cookie-free traffic for the admin Stats page. Counts only."""
    days: list[TrafficDay]  # oldest first; the last is today
    by_view: dict[str, int]  # views of each part of the site over the period, most first
    visitors: int  # unique visitors over the whole period
    returning_visitors: int  # of those, how many had visited before
    counting_since: date_ | None
    sources: list[TrafficSource] = []  # CC-110: most visitors first
    direct: TrafficDirect = TrafficDirect(visitors=0, tickets=0)
    join: JoinStats = JoinStats(visits=0, signups=0, rate=None)  # CC-121


class StatsOut(BaseModel):
    today: date_
    weeks: list[StatsWeek]  # oldest first; the last is the current week so far
    members: int  # every member account (admins left out)
    confirmed_members: int
    active_since: date_ | None  # the first day activity was recorded
    retention: StatsRetention
    photo_checks_month: int = 0  # CC-76: photos checked by Amazon Rekognition this month
    photo_checks_usd: float = 0  # and about what that costs


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
    crews: list[int] = []  # CC-75: the crews it's shared with


class PlanCrewsIn(BaseModel):
    """CC-75: share your plan with exactly these crews of yours ([] = nobody)."""
    crews: list[int] = Field(default_factory=list, max_length=50)


class PhotoOut(ORM):
    """CC-76: a member's photo from an event. url: a link (path on the API) that works for 15 minutes."""
    id: int
    status: str  # waiting | approved | rejected
    reason: str = ""  # an approver's note, when rejected (CC-91)
    created_at: datetime
    url: str = ""


class JournalIn(BaseModel):
    """CC-98: your Cultural Journal entry for an event you've been to: three words (required), then a review
    and whether you'd recommend it (both optional)."""
    words: str = Field(max_length=62)  # exactly three words to describe it, each up to 20 characters
    review: str = Field(default="", max_length=1000)
    recommend: bool = False

    @field_validator("words", "review")
    @classmethod
    def _trimmed(cls, v: str) -> str:
        return v.strip()

    @field_validator("words")
    @classmethod
    def _three_words(cls, v: str) -> str:
        if len(v.split()) != 3:
            raise ValueError("Give three words")
        if any(len(w) > 20 for w in v.split()):
            raise ValueError("Keep each word to 20 characters")
        return " ".join(v.split())


class JournalOut(ORM):
    words: str
    review: str
    recommend: bool


class AttendedOut(ORM):
    event: EventOut
    created_at: datetime
    photo: PhotoOut | None = None  # CC-76: your photo from it, if you added one
    words: str = ""  # CC-98: your journal entry for it (only ever sent to you)
    review: str = ""
    recommend: bool = False


class FoundChange(BaseModel):
    field: str  # cost_text | min_price | begin_date | end_date | weekly
    listed: str | float | dict | None  # what the catalog says
    found: str | float | dict | None  # what the source says now (weekly: show days, {"thu": ["20:00"]})


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
    opera: bool = False  # CC-125: found on an opera source
    venue_name: str
    venue_id: int | None = None
    venue_category: str | None = None  # CC-125: the venue's default, which the approve form starts with
    venue_opera: bool = False
    neighborhood: str = ""
    begin_date: date | None
    end_date: date | None
    open_run: bool
    cost_text: str
    min_price: Decimal | None
    dates_listed: int = 0  # how many performance dates and times the source listed
    dates_timed: bool = False  # CC-88: some of those dates have start times
    weekly: dict = {}  # CC-88: the show days and times the source gave
    weeks: dict = {}  # and which of those days are on some weeks of the month only
    venue_show_times: dict = {}  # the venue's usual show times, to start the form with
    needs: str = ""  # CC-88: "date" or "show_days" while it's missing them; "" when it can be approved as it is
    looked_on: list[str] = []  # the days its own page was read again for what's missing
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


class SubmissionRuleIn(BaseModel):
    """CC-100: an auto-reject rule. "name" takes a phrase; "venue" an existing venue."""
    kind: Literal["name", "venue"]
    phrase: str = Field(default="", max_length=200)
    venue_id: int | None = None


class SubmissionRuleOut(BaseModel):
    id: int
    kind: Literal["name", "venue"]
    phrase: str
    venue_id: int | None
    venue_name: str | None
    caught: int
    created_at: datetime


# ---- culture crews (CC-74) --------------------------------------------------------------

class CrewIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)

    @field_validator("name", "description")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()


class CrewPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=200)


class CrewMemberOut(BaseModel):
    """A member as other members see them: a name, never an email."""
    user_id: uuid.UUID
    name: str
    owner: bool
    you: bool
    joined_at: datetime


class CrewInviteOut(BaseModel):
    id: int
    made_by: str  # the member's name
    yours: bool
    created_at: datetime
    expires_at: datetime
    uses: int


class CrewPlanPerson(BaseModel):
    name: str
    status: PlanStatus
    date: date_ | None
    you: bool


class CrewBeenPerson(BaseModel):
    """CC-93: a crew-mate who's been, and whether their photo from it is approved."""
    name: str
    approved: bool


class CrewBoardItem(BaseModel):
    """CC-75: one event on a crew's board, with who's interested or going (and, CC-93, who's been)."""
    event: EventOut
    people: list[CrewPlanPerson]
    been: list[CrewBeenPerson] = []


class CrewEmailInviteOut(BaseModel):
    """CC-59: an email invite waiting on a crew. The address shows only to whoever sent it."""
    id: int
    made_by: str
    yours: bool
    email: str | None  # only for the member who sent it
    expires_at: datetime


class CrewOut(BaseModel):
    id: int
    name: str
    description: str
    owner: bool  # you own it
    members: list[CrewMemberOut]
    invites: list[CrewInviteOut]  # live invite links
    max_members: int
    plans: list[CrewBoardItem] = []  # CC-75: members' shared plans for upcoming events, soonest first
    email_invites: list[CrewEmailInviteOut] = []  # CC-59: waiting email invites


class CrewInviteLink(BaseModel):
    """A new invite link. The token is shown this once; only its hash is kept."""
    id: int
    token: str
    url: str
    expires_at: datetime


class CrewInvitePreview(BaseModel):
    """What an invite link shows before joining: the crew's name, who invited you, and how many are in it.
    Never who the members are."""
    crew_id: int
    crew_name: str
    description: str
    invited_by: str
    members: int
    already_member: bool
    full: bool


class JoinIn(BaseModel):
    token: str = Field(min_length=10, max_length=100)


class OwnerIn(BaseModel):
    user_id: uuid.UUID


class EmailInviteIn(BaseModel):
    email: EmailStr


class PendingInviteOut(BaseModel):
    """CC-59: an invite waiting for you: the crew, who invited you, how many are in it. Never the members."""
    id: int
    crew_id: int
    crew_name: str
    description: str
    invited_by: str
    members: int
    full: bool
    expires_at: datetime



# ---- issue reports (CC-84) --------------------------------------------------------------

IssueStatus = Literal["new", "looking", "fixed", "wont_fix"]  # a feature request shows them as New, Considering, Done, Not planned (CC-95)
IssueKind = Literal["issue", "feature"]


class IssueIn(BaseModel):
    kind: IssueKind = "issue"  # CC-95: or a feature request (what_happened is then the idea, expected why it'd help)
    what_happened: str = Field(min_length=3, max_length=2000)
    expected: str = Field(default="", max_length=1000)
    page: str = Field(default="", max_length=200)
    browser: str = Field(default="", max_length=300)
    screen: str = Field(default="", max_length=30)
    site_version: str = Field(default="", max_length=40)

    @field_validator("what_happened", "expected", "page", "browser", "screen", "site_version")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()


class IssueOut(ORM):
    """A report as its member sees it: its ticket number, what they wrote, its status, and an admin's note."""
    id: int
    key: str  # the ticket number, ISS-0007 or FR-0002
    kind: IssueKind
    what_happened: str
    expected: str
    page: str
    status: str
    admin_note: str
    created_at: datetime
    updated_at: datetime


class IssueReporter(BaseModel):
    name: str
    email: str


class IssueAdminOut(IssueOut):
    browser: str
    screen: str
    site_version: str
    sent_by: IssueReporter | None = None  # None: the account was deleted


class IssuePage(BaseModel):
    items: list[IssueAdminOut]
    counts: dict[str, int]  # by status, of the kind shown (every kind when there's no filter)
    kinds: dict[str, int] = {}  # CC-95: how many of each kind, open (new or looking) ones only


class IssuePatch(BaseModel):
    status: IssueStatus | None = None
    admin_note: str | None = Field(default=None, max_length=500)


# ---- approvers and approvals (CC-91) ----------------------------------------------

class MemberOut(BaseModel):
    """A member for the admin's Members page."""
    id: uuid.UUID
    display_name: str
    email: str
    role: Role
    approver: bool
    approver_offered: bool


class ApproverAnswerIn(BaseModel):
    accept: bool


class ApprovalOut(BaseModel):
    """A photo for approvers: the member by first name and last initial only, and what Rekognition found."""
    id: int
    status: str  # waiting | approved | rejected
    event: EventRef
    venue: str
    member: str  # "Jordan R."
    labels: list[dict] = []
    blurred: bool
    reason: str = ""
    created_at: datetime
    url: str


class ApprovalsPage(BaseModel):
    items: list[ApprovalOut]
    counts: dict[str, int]  # photos per status


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=300)
