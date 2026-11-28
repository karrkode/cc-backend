"""Database tables. Alembic migrations in alembic/versions must match these."""
import enum
import re
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, CheckConstraint, Date, DateTime, Enum, Float, ForeignKey, ForeignKeyConstraint, Index, Integer,
                        Numeric, String, Text, UniqueConstraint, Uuid, false, func, text, true as sa_true)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .clock import today_in_chicago
from .db import Base



class Role(str, enum.Enum):
    user = "user"
    admin = "admin"


class PlanStatus(str, enum.Enum):
    interested = "interested"
    going = "going"  # with the date they're going on


class EmailTokenPurpose(str, enum.Enum):
    verify_email = "verify_email"
    reset_password = "reset_password"


class Area(str, enum.Enum):
    city = "city"
    suburbs = "suburbs"
    out_of_town = "out_of_town"


class Region(str, enum.Enum):
    """CC-73: the side of town a venue is in, for Plan an outing and the catalog."""
    downtown = "downtown"  # Downtown/Loop
    north = "north"
    west = "west"
    south = "south"
    suburbs = "suburbs"
    out_of_town = "out_of_town"


class EventStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    withdrawn = "withdrawn"  # was on the Google Sheet, then removed from it; hidden but kept


def sort_title(title: str) -> str:
    """How a title files alphabetically: "The Lion King" under L, "(Re)arranging" under R and
    "... before the dawn" under B, ignoring case."""
    t = re.sub(r"^\W+", "", title.strip())  # leading punctuation
    return re.sub(r"^\W+", "", re.sub(r"^(the|a|an)\s+", "", t, flags=re.I)).lower() or title.strip().lower()


def _enum(cls, name):
    # Store the values ("out_of_town"), not the Python names, and give the Postgres type a stable name.
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # always stored lower-case
    password_hash: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(80), default="")
    role: Mapped[Role] = mapped_column(_enum(Role, "user_role"), default=Role.user)
    # Set when they open the link emailed to this address; cleared when the email changes.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # CC-243: when the welcome email went out, so it goes once per account (not again after an email change)
    welcomed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # CC-61: show the dolphin icons on this account's plans. Only the owner's accounts have it
    # (set with scripts.dolphins); everyone else's plans get their own look later.
    dolphins: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-91: approves "I've been" photos. Only an admin can offer it (approver_offered_at), and the member
    # accepts or declines. Admins can always approve.
    approver: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    approver_offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # CC-203: every access token carries this; raising it ends them all at once (sign-out everywhere,
    # a password change or reset), instead of letting them run out their 15 minutes.
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_admin(self) -> bool:  # CC-278: one place for "is an admin" instead of inline role checks
        return self.role == Role.admin

    @property
    def can_approve(self) -> bool:
        return self.approver or self.is_admin

    @property
    def approver_offered(self) -> bool:  # an offer waiting for their answer
        return self.approver_offered_at is not None and not self.approver


class ActiveDay(Base):
    """CC-67: one row for each day (Chicago time) a member used the site while signed in, for the
    admin Stats page (weekly active members, retention). Written when a session starts or refreshes."""
    __tablename__ = "user_active_days"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True, index=True)


class TrafficCount(Base):
    """CC-68: daily totals of what visitors do: views of each part of the site, clicks out to an
    event's ticket page, and outings planned. Counts only."""
    __tablename__ = "traffic_counts"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)  # view | ticket | outing
    name: Mapped[str] = mapped_column(String(20), primary_key=True, default="")  # for views: which part
    count: Mapped[int] = mapped_column(Integer, default=0)


class TrafficVisitor(Base):
    """CC-68: a visitor, known only by a one-way hash of the random ID their browser keeps (see the
    site's privacy note), and the first day it was seen, so later days can count them as returning."""
    __tablename__ = "traffic_visitors"

    visitor: Mapped[str] = mapped_column(String(40), primary_key=True)
    first_day: Mapped[date] = mapped_column(Date)


class TrafficVisitorDay(Base):
    """CC-68: which (hashed) visitors came on which day: unique and returning visitors per day."""
    __tablename__ = "traffic_visitor_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    visitor: Mapped[str] = mapped_column(String(40), primary_key=True)


class TrafficSourceVisit(Base):
    """CC-110: which (hashed) visitors arrived from which source on which day, for visitors by source.
    A source is a tagged link's utm_source / utm_medium / utm_campaign, or else the referring site's
    domain (medium "site"). Never a full address."""
    __tablename__ = "traffic_source_visits"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    visitor: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[str] = mapped_column(String(60), primary_key=True)
    medium: Mapped[str] = mapped_column(String(40), primary_key=True, default="")
    campaign: Mapped[str] = mapped_column(String(60), primary_key=True, default="")


class TrafficSourceCount(Base):
    """CC-110: daily ticket clicks by the source that brought the visitor in this visit. Counts only."""
    __tablename__ = "traffic_source_counts"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    source: Mapped[str] = mapped_column(String(60), primary_key=True)
    medium: Mapped[str] = mapped_column(String(40), primary_key=True, default="")
    campaign: Mapped[str] = mapped_column(String(60), primary_key=True, default="")
    tickets: Mapped[int] = mapped_column(Integer, default=0)


class TrafficJoinVisit(Base):
    """CC-121: which (hashed) visitors opened the join page (/join) on which day, and the source that brought
    them (CC-110's, or "" for direct / unknown). Visits to the join page by source."""
    __tablename__ = "traffic_join_visits"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    visitor: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[str] = mapped_column(String(60), primary_key=True, default="")
    medium: Mapped[str] = mapped_column(String(40), primary_key=True, default="")
    campaign: Mapped[str] = mapped_column(String(60), primary_key=True, default="")


class TrafficJoinSignup(Base):
    """CC-121: daily sign-ups in visits that came by the join page, by that visit's source. Counts only:
    nothing ties a count to the account."""
    __tablename__ = "traffic_join_signups"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    source: Mapped[str] = mapped_column(String(60), primary_key=True, default="")
    medium: Mapped[str] = mapped_column(String(40), primary_key=True, default="")
    campaign: Mapped[str] = mapped_column(String(60), primary_key=True, default="")
    signups: Mapped[int] = mapped_column(Integer, default=0)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex; the raw token is never stored
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # CC-203: when the session began with a password, carried over on each refresh, so a session ends
    # after max_session_days however often it refreshes.
    signed_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailToken(Base):
    """A single-use link sent by email: confirm an address, or reset a password.
    Only the SHA-256 of the token is stored, like refresh tokens."""
    __tablename__ = "email_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[EmailTokenPurpose] = mapped_column(_enum(EmailTokenPurpose, "email_token_purpose"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(320))  # the address it was sent to; must still be theirs
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Venue(Base):
    __tablename__ = "venues"
    # Same name, different neighborhood = different location (e.g. The Second City runs two).
    __table_args__ = (UniqueConstraint("name", "neighborhood", name="uq_venues_name_neighborhood"),
                      CheckConstraint("standard_min_price IS NULL OR standard_min_price >= 0",
                                      name="ck_venues_standard_min_price_not_negative"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    neighborhood: Mapped[str] = mapped_column(String(120), default="")
    area: Mapped[Area] = mapped_column(_enum(Area, "venue_area"), default=Area.city)
    # CC-73: worked out from the neighborhood (app/areas.py region_of); admins can change it. Null when
    # a city neighborhood isn't in the list yet: the Venues page lists those to fix.
    region: Mapped[Region | None] = mapped_column(_enum(Region, "venue_region"), nullable=True)
    # CC-78: stored for Find an outing (CC-79), not shown on cards. Weekdays are mon..sun, times HH:MM (24-hour).
    # open_hours: {"tue": ["10:00", "17:00"], "wed": null (closed)}; a day left out isn't known.
    # show_times: usual performance start times, {"wed": ["19:30"], "sun": ["14:00", "19:30"]}; no key = no shows.
    # *_by: who filled it in: "" (nobody), "discovery" (the venue's own site), or "admin". Discovery
    # never replaces what an admin set.
    open_hours: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    open_hours_by: Mapped[str] = mapped_column(String(20), default="", server_default="")
    show_times: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    show_times_by: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # CC-89: what it usually costs ("Free", "$25 general admission"), shown on its events that don't post a price,
    # and used by the price filters and sort; never copied into events. standard_min_price: 0 = free, NULL = none set
    standard_cost: Mapped[str] = mapped_column(String(120), default="", server_default="")
    standard_min_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    # CC-125: the category new events here start with (null: none yet), and whether they start as opera
    category: Mapped[str | None] = mapped_column(String(40))
    opera: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-213: the venue's own site, normalised (app/websites.py); "" = none. Admin-only for now: only the admin
    # venue routes read or write it, and it's set only by an admin (typed in, or a confirmed suggestion).
    website: Mapped[str] = mapped_column(String(500), default="", server_default="")
    # CC-218: the public address (app/addresses.py): the street, city and ZIP, all three or none ("" = none). Optional
    # here because discovery and members' suggestions add venues without one. Set only by an admin: discovery can only suggest.
    address: Mapped[str] = mapped_column(String(200), default="", server_default="")
    city: Mapped[str] = mapped_column(String(120), default="", server_default="")
    zip_code: Mapped[str] = mapped_column(String(10), default="", server_default="")
    # CC-188: added by a member's submission and not yet accepted by an admin (app/venuereview.py)
    pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-219: an admin took it out of the public venue list. Not pending: approving an event ends pending, but only an
    # admin ends this. Its events stay listed, and discovery and submissions still file events under it.
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-221: an admin deleted its discovery source, so discovery doesn't add one back from its website
    no_source: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class VenueWebsiteDismissal(Base):
    """CC-213: a suggested website an admin dismissed for a venue, so it isn't suggested again."""
    __tablename__ = "venue_website_dismissals"

    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id", ondelete="CASCADE"), primary_key=True)
    url: Mapped[str] = mapped_column(String(500), primary_key=True)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VenueAddressSuggestion(Base):
    """CC-218: a street address seen in a page's event data, for an admin to confirm. Unlike a website it can't be
    worked out when asked for, since it comes from pages read earlier. A dismissed one stays, so it isn't added again."""
    __tablename__ = "venue_address_suggestions"

    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id", ondelete="CASCADE"), primary_key=True)
    address: Mapped[str] = mapped_column(String(200), primary_key=True)  # the street
    city: Mapped[str] = mapped_column(String(120), default="", server_default="")  # "" = the page didn't say
    zip_code: Mapped[str] = mapped_column(String(10), default="", server_default="")
    source: Mapped[str] = mapped_column(String(200), default="", server_default="")  # the discovery source's name
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("end_date IS NULL OR begin_date IS NULL OR end_date >= begin_date",
                        name="ck_events_dates_in_order"),
        CheckConstraint("min_price IS NULL OR min_price >= 0", name="ck_events_min_price_not_negative"),
        Index("ix_events_status_end_date", "status", "end_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The event's id on the old Google Sheet (retired in CC-72): old share links use it, and seeding matches on it.
    source_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    sort_title: Mapped[str] = mapped_column(String(300), index=True)  # kept in step with title, see below
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id", ondelete="RESTRICT"), index=True)
    begin_date: Mapped[date | None] = mapped_column(Date)  # NULL = start date not announced
    end_date: Mapped[date | None] = mapped_column(Date)
    open_run: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_text: Mapped[str] = mapped_column(String(120), default="")
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))  # NULL = price not posted, 0 = free
    category: Mapped[str] = mapped_column(String(40))  # CC-125: one of app.categories.CATEGORIES
    # CC-125: an opera (category Music), also listed under Theater; only admins set it
    opera: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[EventStatus] = mapped_column(_enum(EventStatus, "event_status"), default=EventStatus.pending)
    # An approved event with a future publish_at stays hidden until then (admins can schedule events).
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # The submitter ticked "credit me" for this event; without it their name is never shown publicly.
    credit_me: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-79: when it's on, for Find an outing. performances: specific dates and times from event discovery,
    # [{"date": "2026-11-05", "time": "19:30"}]. CC-301: FROZEN, nothing reads or writes it any more; its dates were
    # copied into event_run_dates (migration 0060) and live there as `shows`. A later story drops the column.
    # weekly: a usual pattern an admin sets, {"thu": ["19:30"]}, which overrides the venue's show times.
    # Neither is shown on cards.
    performances: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    weekly: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # CC-88: weekly days that are only on some weeks of the month, {"thu": [1, 3]} (1-4: first to fourth, 5: the last);
    # a day left out is every week
    weeks: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # CC-71: the event source it was found on, when it was approved from discovery
    found_by_id: Mapped[int | None] = mapped_column(ForeignKey("discovery_sources.id", ondelete="SET NULL"))
    # CC-100: why an auto-reject rule rejected it ("" otherwise), so admins can find it and approve it after all
    reject_reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # CC-133: set when removing Comedy and Educational withdrew it (its category, or its venue's name), for the
    # admin's review; cleared when an admin brings it back
    withdrawn_reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # CC-250: the event's photo, a WebP in the event-images bucket served by the site's CloudFront at /<key>
    # (app/event_images.py). NULL shows the category water. image_source_url is the page or file it came from,
    # so sourcing can tell which events still need one and an admin can check where it came from.
    image_key: Mapped[str | None] = mapped_column(String(200))
    image_source_url: Mapped[str] = mapped_column(Text, default="", server_default="")
    # CC-265: a manual removal or replacement must not be overwritten by automatic sourcing.
    image_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-246: sourcing's looks for its picture (app/discovery/pictures.py): how many, and when the last one was. Events
    # from before CC-246 start at the most, since CC-250's search already looked for theirs.
    picture_tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    picture_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())

    venue: Mapped[Venue] = relationship(lazy="joined")
    submitter: Mapped[User | None] = relationship(lazy="selectin")  # one extra query per batch, not per event
    # CC-301: the Show Run: every performance, one row each (a day can have several). Two kinds, told apart by
    # EventRunDate.origin: an exact Show Run (any show a person saved: origin admin or member), on those dates only,
    # with begin_date and end_date always its first and last show; or a found-only one (origin found, written by
    # sourcing), which behaves as `performances` did: exact up to its last date, then the weekly pattern.
    # One event never mixes the two: a form save replaces every row with admin rows, and sourcing writes only when
    # there are no rows or every row is found. (CC-96's fixed run is an exact Show Run; same table.)
    shows: Mapped[list["EventRunDate"]] = relationship(lazy="selectin", cascade="all, delete-orphan",
                                                       order_by="EventRunDate.date, EventRunDate.time")

    @property
    def show_run_exact(self) -> bool:
        """CC-301: the event is on its shows' dates only (a person saved them), not on top of a weekly pattern."""
        return any(r.origin != FOUND for r in self.shows)

    @property
    def run_dates(self) -> list["EventRunDate"]:
        """CC-96, LEGACY (CC-301): the shows of an exact Show Run that have a time. What the live site reads until the
        new one ships; found shows and untimed ones are left out, so old pages see exactly what they used to."""
        return [r for r in self.shows if r.origin != FOUND and r.time] if self.show_run_exact else []

    @validates("title")
    def _keep_sort_title(self, _key, value):
        self.sort_title = sort_title(value)
        return value

    @property
    def submitted_by_name(self) -> str | None:
        """The submitter's display name, but only if they chose to be credited for this event."""
        if self.credit_me and self.submitter is not None and self.submitter.display_name.strip():
            return self.submitter.display_name.strip()
        return None

    @property
    def image_url(self) -> str | None:
        """CC-250: the photo's address on the site, or None for the category water."""
        from .event_images import url_for  # here, so models stay free of storage code at import time
        return url_for(self.image_key)

    @property
    def image_credit_url(self) -> str | None:
        """CC-246: the page the photo came from, credited on the event page; None without a photo or a page."""
        return self.image_source_url or None if self.image_key else None

    # CC-66: set per request by app.picks.attach_picks (not stored); the owner's public pick, if any.
    pick = None
    # CC-75: set per request by app.crewplans.attach_crew_plans (not stored); your crew-mates' shared plans.
    crew_plans = ()
    # CC-112: set per request by app.takes.attach_takes (not stored); members' shared journal entries.
    takes = ()
    # CC-301: set per request on single-event responses (not stored): every show, past ones too, as EventOut.show_run.
    show_run = ()

    @property
    def schedule(self) -> dict:
        """CC-79: what Find an outing needs to know when it's on: upcoming dates and times, the weekly pattern
        (the event's own, else its venue's show times), and the venue's opening hours."""
        today = today_in_chicago().isoformat()
        # CC-301: upcoming shows from the one store; time is null when it isn't posted
        dates = [{"date": r.date.isoformat(), "time": r.time or None} for r in self.shows if r.date.isoformat() >= today]
        dates = sorted(dates, key=lambda p: (p["date"], p["time"] or ""))[:SCHEDULE_DATES_MAX]
        own = bool(self.weekly)
        venue = self.venue
        return {"dates": dates, "weekly": self.weekly if own else (venue.show_times if venue else {}) or {},
                "weeks": (self.weeks or {}) if own else {},
                "weekly_from": "event" if own else ("venue" if venue and venue.show_times else ""),
                # CC-301: where the dates came from: "event" (a person's exact Show Run), "found" (sourcing's), or ""
                "dates_from": "event" if self.show_run_exact else ("found" if self.shows else ""),
                "hours": (venue.open_hours if venue else {}) or {}}


class EventRunDate(Base):
    """CC-301: one show of an event's Show Run (CC-96's fixed-run date, grown up): its day, start time and, if known,
    end time. Several shows can share a day. time may be "" (not posted yet). origin: admin (saved in a form by an
    admin), member (sent in by a member) or found (written by sourcing). The table keeps its CC-96 name."""
    __tablename__ = "event_run_dates"
    __table_args__ = (UniqueConstraint("event_id", "date", "time", name="uq_event_run_dates_show"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    time: Mapped[str] = mapped_column(String(5), default="")  # 24-hour HH:MM, or "" when not posted
    end_time: Mapped[str] = mapped_column(String(5), default="", server_default="")
    origin: Mapped[str] = mapped_column(String(6), default="admin", server_default="admin")  # admin | member | found


FOUND = "found"  # CC-301: EventRunDate.origin of the dates sourcing wrote
SCHEDULE_DATES_MAX = 500  # CC-301: upcoming shows in Event.schedule (the Show Run itself is capped at the same number)


class Attended(Base):
    __tablename__ = "attended"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # CC-98: the member's Cultural Journal entry for it, all optional and private to them; it goes with the mark.
    words: Mapped[str] = mapped_column(String(62), default="", server_default="")
    review: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    recommend: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-112: a public take. shared_at: when the member shared the entry on the event's page (null: private);
    # anonymous: shown as "A Chi Culture Crew member"; hidden: an admin took it down (the member isn't told).
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    event: Mapped[Event] = relationship(lazy="joined")
    photo = None  # CC-76: set per request (the member's own photo, as PhotoOut)

    @property
    def shared(self) -> bool:
        return self.shared_at is not None


class WentPhoto(Base):
    """CC-76: a member's photo from an event they've been to (one per mark), waiting for an approver
    or decided. labels: what Amazon Rekognition found; blurred: shown blurred to approvers."""
    __tablename__ = "went_photos"
    __table_args__ = (ForeignKeyConstraint(["user_id", "event_id"], ["attended.user_id", "attended.event_id"], ondelete="CASCADE"),
                      UniqueConstraint("user_id", "event_id", name="uq_went_photos_mark"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)  # where it's stored (S3 key, or a local path)
    content_type: Mapped[str] = mapped_column(String(40))
    size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="waiting", index=True)  # waiting | approved | rejected
    blurred: Mapped[bool] = mapped_column(Boolean, default=False)
    labels: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(String(300), default="")  # an approver's note to the member (CC-91)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PhotoCheck(Base):
    """CC-76: one Rekognition check of an uploaded photo, kept for the daily upload limit and the monthly cost.
    outcome: stored | refused (refused photos are never stored) | failed (checked, then not saved: it still counts)
    | checking (CC-197: reserved before Rekognition is called; it holds the daily limit)."""
    __tablename__ = "photo_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    outcome: Mapped[str] = mapped_column(String(20))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class JournalCategory(Base):
    """CC-248: a category a member made for their own journal events. Private to them; the catalog keeps its three."""
    __tablename__ = "journal_categories"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_journal_categories_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OwnEvent(Base):
    """CC-248: an event a member added to their own Cultural Journal because it isn't in the catalog. Private to
    them: never shared, never on a public page, never shown to crews. A catalog category or one of their own (or
    neither). Its photo is checked by Rekognition but has no approver, since only its member ever sees it."""
    __tablename__ = "own_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    on_date: Mapped[date] = mapped_column(Date)
    venue: Mapped[str] = mapped_column(String(120), default="", server_default="")
    category: Mapped[str | None] = mapped_column(String(40))  # one of app.categories.CATEGORIES
    category_id: Mapped[int | None] = mapped_column(ForeignKey("journal_categories.id", ondelete="SET NULL"), index=True)
    words: Mapped[str] = mapped_column(String(62), default="", server_default="")
    review: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    recommend: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    photo_key: Mapped[str | None] = mapped_column(String(200), unique=True)  # where it's stored, as WentPhoto.key
    photo_type: Mapped[str] = mapped_column(String(40), default="", server_default="")
    photo_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    own_category: Mapped[JournalCategory | None] = relationship(lazy="joined")


class Plan(Base):
    """A user's own plan for an event (CC-53): interested, or going on a date. Private to them unless
    they share it with crews they're in (PlanShare, CC-75)."""
    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("(status = 'going') = (going_on IS NOT NULL)", name="ck_plans_going_has_a_date"),
        CheckConstraint("going_at IS NULL OR status = 'going'", name="ck_plans_time_only_when_going"),
        Index("ix_plans_user_status", "user_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[PlanStatus] = mapped_column(_enum(PlanStatus, "plan_status"))
    going_on: Mapped[date | None] = mapped_column(Date)
    going_at: Mapped[str | None] = mapped_column(String(5))  # CC-264: the show's start time that day, 24-hour HH:MM
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())

    event: Mapped[Event] = relationship(lazy="joined")


class Crew(Base):
    """A culture crew (CC-74): people who go out together. Members see each other's names, never emails;
    nobody outside the crew can see who's in it."""
    __tablename__ = "crews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(String(200), default="")
    # Deleting the owner's account deletes the crew (there's no self-service account deletion yet; when there
    # is, hand the crew to its longest-standing member first).
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    members: Mapped[list["CrewMember"]] = relationship(back_populates="crew", cascade="all, delete-orphan",
                                                       order_by="CrewMember.joined_at", lazy="selectin")


class CrewMember(Base):
    __tablename__ = "crew_members"

    crew_id: Mapped[int] = mapped_column(ForeignKey("crews.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    crew: Mapped[Crew] = relationship(back_populates="members")
    user: Mapped[User] = relationship(lazy="joined")


class DatePoll(Base):
    """CC-297: a crew picking a date to see one event together. Only the crew's members ever see it."""
    __tablename__ = "date_polls"
    __table_args__ = (
        # One open poll per crew per event. Partial, so a locked or cancelled one doesn't block the next.
        Index("uq_date_polls_one_open", "crew_id", "event_id", unique=True,
              postgresql_where=text("status = 'open'"), sqlite_where=text("status = 'open'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crew_id: Mapped[int] = mapped_column(ForeignKey("crews.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    started_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(10), default="shows", server_default="shows")  # "shows" (CC-297) or "hours" (CC-298: an exhibition's days)
    status: Mapped[str] = mapped_column(String(10), default="open", server_default="open")  # open / locked / cancelled
    window_from: Mapped[date] = mapped_column(Date)
    window_to: Mapped[date] = mapped_column(Date)
    locked_option_id: Mapped[int | None] = mapped_column(Integer)  # no FK: options cascade with the poll
    locked_start: Mapped[str | None] = mapped_column(String(5))  # CC-298: an hours poll locks a range of the day, "13:00" to "15:30"
    locked_end: Mapped[str | None] = mapped_column(String(5))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    options: Mapped[list["DatePollOption"]] = relationship(cascade="all, delete-orphan", order_by="DatePollOption.date, DatePollOption.time",
                                                           lazy="selectin")
    event: Mapped["Event"] = relationship(lazy="joined")


class DatePollOption(Base):
    __tablename__ = "date_poll_options"
    __table_args__ = (UniqueConstraint("poll_id", "date", "time", name="uq_date_poll_options"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("date_polls.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    time: Mapped[str] = mapped_column(String(5), default="", server_default="")  # HH:MM; "" for a day of an hours poll (CC-298) or a show with no time
    # CC-298: an hours poll keeps the venue's hours for the day as they were when it started, so a later change can't strand answers
    opens: Mapped[str | None] = mapped_column(String(5))
    closes: Mapped[str | None] = mapped_column(String(5))


class DatePollAnswer(Base):
    """That this member has answered, even with no showtime ticked ("none of these work")."""
    __tablename__ = "date_poll_answers"

    poll_id: Mapped[int] = mapped_column(ForeignKey("date_polls.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DatePollPick(Base):
    """An option that works for a member."""
    __tablename__ = "date_poll_picks"

    option_id: Mapped[int] = mapped_column(ForeignKey("date_poll_options.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)


class DatePollBlock(Base):
    """CC-298: a half-hour of an hours poll's day that works for a member (start "13:30" means 1:30 to 2:00 PM)."""
    __tablename__ = "date_poll_blocks"

    option_id: Mapped[int] = mapped_column(ForeignKey("date_poll_options.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    start: Mapped[str] = mapped_column(String(5), primary_key=True)


class CrewRemoval(Base):
    """CC-187: a leader removed this account from this crew. Links and other members' invites can't bring it back; the
    row goes when the account accepts an email invite from the crew's leader."""
    __tablename__ = "crew_removals"

    crew_id: Mapped[int] = mapped_column(ForeignKey("crews.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    removed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    removed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrewInvite(Base):
    """An invite link: anyone with it can join until it expires or is revoked. Only its hash is kept;
    the link itself is shown once, to the member who made it."""
    __tablename__ = "crew_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crew_id: Mapped[int] = mapped_column(ForeignKey("crews.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uses: Mapped[int] = mapped_column(Integer, default=0)

    creator: Mapped[User | None] = relationship(lazy="joined")


class CrewEmailInvite(Base):
    """CC-59: an invite to a crew by email address. It shows to whichever account has that address (now, or
    once someone registers with it) until it's accepted, declined, cancelled or 14 days pass. The inviter
    never learns whether the address has an account."""
    __tablename__ = "crew_email_invites"
    __table_args__ = (UniqueConstraint("crew_id", "email", name="uq_crew_email_invite"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crew_id: Mapped[int] = mapped_column(ForeignKey("crews.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)  # lower-case, as users.email
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    crew: Mapped[Crew] = relationship(lazy="joined")
    inviter: Mapped[User | None] = relationship(lazy="joined")


class SentEmail(Base):
    """CC-184: one email the site sent (or queued) because of something a member or visitor did, for the caps in
    app/emailcaps.py. Nothing else deletes these rows, so cancelling an invite doesn't make room to send another."""
    __tablename__ = "sent_emails"
    __table_args__ = (Index("ix_sent_emails_email_sent_at", "email", "sent_at"),
                      Index("ix_sent_emails_account_kind_sent_at", "account_id", "kind", "sent_at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320))  # lower-case, as users.email
    kind: Mapped[str] = mapped_column(String(20))  # emailcaps.VERIFY, RESET or INVITE
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))  # who caused it
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanShare(Base):
    """CC-75: a plan shared with one of its owner's crews. Only that crew's members see it. Goes when the
    plan does (cleared, or "I've been"), when the crew does, and when its owner leaves the crew."""
    __tablename__ = "plan_shares"
    __table_args__ = (ForeignKeyConstraint(["user_id", "event_id"], ["plans.user_id", "plans.event_id"], ondelete="CASCADE"),)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    crew_id: Mapped[int] = mapped_column(ForeignKey("crews.id", ondelete="CASCADE"), primary_key=True, index=True)
    # CC-240: when it was shared, for the console's weekly count. Null on shares made before this column existed.
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AttendedShare(Base):
    """CC-93: an "I've been" mark seen by one of its owner's crews: the crews the plan was shared with when
    they marked it. Goes with the mark, when the crew does, and when its owner leaves the crew."""
    __tablename__ = "attended_shares"
    __table_args__ = (ForeignKeyConstraint(["user_id", "event_id"], ["attended.user_id", "attended.event_id"], ondelete="CASCADE"),)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    crew_id: Mapped[int] = mapped_column(ForeignKey("crews.id", ondelete="CASCADE"), primary_key=True, index=True)


class IssueReport(Base):
    """CC-84: a signed-in member's report of a problem with the site. Admins set its status and can add a
    note the member sees beside it on Your account. CC-95: or a feature request (kind "feature"); each kind
    is numbered on its own (ISS-0007, FR-0002)."""
    __tablename__ = "issue_reports"
    __table_args__ = (UniqueConstraint("kind", "number", name="uq_issue_reports_kind_number"),
                      UniqueConstraint("jira_key", name="uq_issue_reports_jira_key"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), default="issue", server_default="issue", index=True)  # issue | feature
    number: Mapped[int] = mapped_column(Integer)  # within its kind; reports from before CC-95 kept their ISS- numbers
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    what_happened: Mapped[str] = mapped_column(Text)
    expected: Mapped[str] = mapped_column(Text, default="")
    page: Mapped[str] = mapped_column(String(200), default="")  # the view they were on, e.g. "Catalog"
    browser: Mapped[str] = mapped_column(String(300), default="")
    screen: Mapped[str] = mapped_column(String(30), default="")
    site_version: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new | looking | fixed | wont_fix
    admin_note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # CC-241: the Jira ticket an admin made from it (admins only). jira_pending_at is set while one is being made,
    # so a second click or a retry can't make another; a claim older than a minute has failed and may be retried.
    jira_key: Mapped[str | None] = mapped_column(String(20))
    jira_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    jira_pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # CC-245: when it was last set to fixed or won't fix (cleared if it's reopened), for the dashboard's time to fix.
    # A note edit changes updated_at, so updated_at can't say this.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    reporter: Mapped[User | None] = relationship(lazy="joined")

    @property
    def key(self) -> str:
        """Its ticket number, e.g. ISS-0007 or FR-0002: told to the member, and searchable on the Issues page."""
        return f"{'FR' if self.kind == 'feature' else 'ISS'}-{self.number:04d}"


class SheetSync(Base):
    """One run of the nightly Google Sheet sync. The sync was retired in October 2026 (CC-72); the table
    keeps its history and nothing writes to it."""
    __tablename__ = "sheet_syncs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str] = mapped_column(String(20))  # ok | failed | dry_run
    rows_read: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    withdrawn: Mapped[int] = mapped_column(Integer, default=0)
    restored: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")



# ---- event discovery (CC-70) ----------------------------------------------------------
# A source is a venue or company whose own pages are checked now and then for new events. What a
# check finds goes into DiscoveredItem, a holding list an admin reviews (CC-71); nothing found here
# reaches the live catalog by itself.

class SourceKind(str, enum.Enum):
    theater = "theater"
    opera = "opera"
    museum = "museum"
    music = "music"
    comedy = "comedy"


class DiscoverySource(Base):
    __tablename__ = "discovery_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"))
    pages: Mapped[list] = mapped_column(JSON, default=list)  # the events pages to read, in order
    reader: Mapped[str] = mapped_column(String(20), default="claude")  # claude | aic_api (Art Institute's own API)
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venues.id", ondelete="SET NULL"))
    # Where its events are, when there's no venue yet: used to add one when an event is approved.
    venue_name: Mapped[str] = mapped_column(String(200), default="")
    neighborhood: Mapped[str] = mapped_column(String(120), default="")
    every_days: Mapped[int] = mapped_column(Integer, default=30)  # the usual gap between checks
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_true())
    next_check_on: Mapped[date | None] = mapped_column(Date)  # due on or after this day; None = due now
    unchanged_streak: Mapped[int] = mapped_column(Integer, default=0)  # checks in a row with nothing new
    # CC-198: reads in a row that cost tokens and gave nothing usable; each one doubles the wait
    failed_reads: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    page_state: Mapped[dict] = mapped_column(JSON, default=dict)  # per page: etag, last_modified, text hash
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(20), default="")  # ok | unchanged | blocked | error
    last_error: Mapped[str] = mapped_column(Text, default="")
    venue_hours: Mapped[dict] = mapped_column(JSON, default=dict)  # what the venue's site says, for CC-78
    # CC-81: "Check by hand only": a site that can't be read isn't tried automatically any more, but stays on
    # the recap's "Check these by hand" list (unlike a source that's turned off)
    manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-80: an admin pressed Check now; such checks still run when the monthly budget is used up
    check_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-221: added from a venue's website (app/discovery/venuesources.py). added_from keeps that address, because the
    # pages move to the calendar link; find_link means the home page hasn't been searched for that link yet.
    added_from: Mapped[str] = mapped_column(String(500), default="", server_default="")
    find_link: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    venue: Mapped["Venue | None"] = relationship(lazy="joined")


class DiscoveredItem(Base):
    """An event a source listed: new to us, already in the catalog ("listed"), or a proposed update
    to a listed event. Admins approve, reject or dismiss it (CC-71)."""
    __tablename__ = "discovered_items"
    __table_args__ = (UniqueConstraint("source_id", "key", name="uq_discovered_source_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # CC-133: kept, with its review status, when an admin deletes its source (then it has none)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("discovery_sources.id", ondelete="SET NULL"), index=True)
    key: Mapped[str] = mapped_column(String(500))  # the event's own page, or its normalized title
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new | listed | update | approved | rejected | skipped | hidden (CC-207)
    skip_reason: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(2000), default="")
    kind: Mapped[str] = mapped_column(String(30), default="")  # production, exhibition, talk, ... as read
    category: Mapped[str | None] = mapped_column(String(40))
    opera: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # CC-125: from an opera source
    venue_name: Mapped[str] = mapped_column(String(200), default="")
    begin_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    open_run: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_text: Mapped[str] = mapped_column(String(120), default="")
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    performances: Mapped[list] = mapped_column(JSON, default=list)  # [{"date": "2026-11-05", "time": "19:30"}]
    changes: Mapped[dict] = mapped_column(JSON, default=dict)  # for "update": {field: [listed, found]}
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"), index=True)
    detail_checks: Mapped[list] = mapped_column(JSON, default=list)  # pre-opening checks done: [60, 45]
    # CC-88: the show days and times the page gives ({"thu": ["20:00"]}), and the days its own page was read
    # again because a date or show days were missing (once after it's found, then weekly for 4 weeks)
    weekly: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    looked_on: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    # CC-302: what the last look for exact dates on a tickets or calendar page found ("Refused: <address>: why", or why none)
    dates_note: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    # CC-304: when the stronger model last looked for this show's dates. One look per show: it's the costly one
    strong_looked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    weeks: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")  # some weeks of the month only, as on events
    # CC-310: the candidate picture the admin sees before approving (copied to our storage, never hotlinked), why there
    # isn't one, and whether the admin chose (uploaded or removed): then sourcing never touches it
    image_key: Mapped[str | None] = mapped_column(String(200))
    image_source_url: Mapped[str] = mapped_column(Text, default="", server_default="")
    image_note: Mapped[str] = mapped_column(String(300), default="", server_default="")
    image_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    image_tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    image_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # CC-320: which version of the picture finder (pictures.FINDER_VERSION) last looked. 0 = before it existed, so an item
    # an older finder missed gets one more look without rewriting any row.
    image_finder: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # CC-257: a hidden item an admin delayed; it goes back to "new" once this moment has passed (store.release_delayed)
    delayed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped[DiscoverySource | None] = relationship(lazy="joined")


class VenueSuggestion(Base):
    """CC-209: a member's suggestion of a venue's calendar link. An admin accepts it (which adds a discovery source) or
    declines it. The member's id is kept for the admin and the member's own list, never shown publicly."""
    __tablename__ = "venue_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    url: Mapped[str] = mapped_column(String(500))
    venue_name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    note: Mapped[str] = mapped_column(String(500), default="", server_default="")
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending", index=True)  # pending | accepted | declined
    decline_reason: Mapped[str] = mapped_column(String(200), default="", server_default="")  # the admin's, shown to the member
    source_id: Mapped[int | None] = mapped_column(ForeignKey("discovery_sources.id", ondelete="SET NULL"))  # what accepting added
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User | None"] = relationship(lazy="joined")


class AppSetting(Base):
    """A site-wide setting an admin can change, by key (CC-80: discovery_budget_usd)."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class SkipRule(Base):
    """"Reject and skip similar" (CC-71): future items whose title contains the phrase are rejected
    quietly, for one source or all of them."""
    __tablename__ = "discovery_skip_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phrase: Mapped[str] = mapped_column(String(200))  # normalized, as rules.norm_title makes it
    source_id: Mapped[int | None] = mapped_column(ForeignKey("discovery_sources.id", ondelete="CASCADE"))  # NULL = all
    caught: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped[DiscoverySource | None] = relationship(lazy="joined")


class SubmissionRule(Base):
    """CC-100: auto-reject rules for members' submissions (kept apart from the discovery skip rules).
    "name": the title contains the phrase, normalized as rules.norm_title makes it. "venue": at that venue."""
    __tablename__ = "submission_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # name | venue
    phrase: Mapped[str] = mapped_column(String(200), default="")
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venues.id", ondelete="CASCADE"))
    caught: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    venue: Mapped[Venue | None] = relationship(lazy="joined")


class DiscoveryCheck(Base):
    """One check of one source (or one event's page): what happened and what it cost. Feeds the
    sources list, the weekly recap (CC-77) and the monthly cost."""
    __tablename__ = "discovery_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # CC-124: no source for a member's link (what = "link"); its Claude reads still count toward the budget
    # CC-133: a deleted source's checks are kept too, so the month's spend still counts them
    source_id: Mapped[int | None] = mapped_column(ForeignKey("discovery_sources.id", ondelete="SET NULL"), index=True, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    what: Mapped[str] = mapped_column(String(20), default="source")  # source | detail (one event's page) | link
    status: Mapped[str] = mapped_column(String(20))  # ok | unchanged | blocked | error
    new_items: Mapped[int] = mapped_column(Integer, default=0)
    updates: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    pages_read: Mapped[int] = mapped_column(Integer, default=0)  # pages that had to go to the model
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # CC-304: what the stronger model's reads cost, in dollars. Its tokens aren't in the two columns above, which are priced
    # as Haiku's; the budget, the recap and the source ordering add this on top
    strong_usd: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    note: Mapped[str] = mapped_column(Text, default="")


class RequestMetric(Base):
    """CC-244: one API request, for the admin Site health page: when, which route (its pattern, never the real path),
    the status and how long it took. No address, user, query or body is kept. Rows go after 30 days (app/health.py)."""
    __tablename__ = "request_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    method: Mapped[str] = mapped_column(String(8))
    route: Mapped[str] = mapped_column(String(200))  # e.g. /events/{event_id}; "(no route)" when nothing matched
    status: Mapped[int] = mapped_column(Integer, index=True)
    ms: Mapped[float] = mapped_column(Float)


class DbCheck(Base):
    """CC-244: the API's own check of the database each minute: did it answer, and how fast. Kept 30 days."""
    __tablename__ = "db_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    ms: Mapped[float | None] = mapped_column(Float, nullable=True)
