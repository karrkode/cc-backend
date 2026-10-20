"""Database tables. Alembic migrations in alembic/versions must match these."""
import enum
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import (JSON, Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint, Uuid, false, func, true as sa_true)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .db import Base


_CHICAGO = ZoneInfo("America/Chicago")  # the site's day (app.stats has the same)

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
    # CC-61: show the dolphin icons on this account's plans. Only the owner's accounts have it
    # (set with scripts.dolphins); everyone else's plans get their own look later.
    dolphins: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None


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


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex; the raw token is never stored
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    __table_args__ = (UniqueConstraint("name", "neighborhood", name="uq_venues_name_neighborhood"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    neighborhood: Mapped[str] = mapped_column(String(120), default="")
    area: Mapped[Area] = mapped_column(_enum(Area, "venue_area"), default=Area.city)
    # CC-73: worked out from the neighborhood (app/areas.py region_of); admins can change it. Null when
    # a city neighborhood isn't in the list yet: the Venues page lists those to fix.
    region: Mapped[Region | None] = mapped_column(_enum(Region, "venue_region"), nullable=True)
    # CC-78: stored for the planner (CC-79), not shown on cards. Weekdays are mon..sun, times HH:MM (24-hour).
    # open_hours: {"tue": ["10:00", "17:00"], "wed": null (closed)}; a day left out isn't known.
    # show_times: usual performance start times, {"wed": ["19:30"], "sun": ["14:00", "19:30"]}; no key = no shows.
    # *_by: who filled it in: "" (nobody), "discovery" (the venue's own site), or "admin". Discovery
    # never replaces what an admin set.
    open_hours: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    open_hours_by: Mapped[str] = mapped_column(String(20), default="", server_default="")
    show_times: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    show_times_by: Mapped[str] = mapped_column(String(20), default="", server_default="")


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
    category: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[EventStatus] = mapped_column(_enum(EventStatus, "event_status"), default=EventStatus.pending)
    # An approved event with a future publish_at stays hidden until then (admins can schedule events).
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # The submitter ticked "credit me" for this event; without it their name is never shown publicly.
    credit_me: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # CC-79: when it's on, for the planner. performances: specific dates and times from event discovery,
    # [{"date": "2026-11-05", "time": "19:30"}], kept current by discovery. weekly: a usual pattern an admin
    # sets, {"thu": ["19:30"]}, which overrides the venue's show times. Neither is shown on cards.
    performances: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    weekly: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # CC-71: the event source it was found on, when it was approved from discovery
    found_by_id: Mapped[int | None] = mapped_column(ForeignKey("discovery_sources.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())

    venue: Mapped[Venue] = relationship(lazy="joined")
    submitter: Mapped[User | None] = relationship(lazy="selectin")  # one extra query per batch, not per event

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

    # CC-66: set per request by app.picks.attach_picks (not stored); the owner's public pick, if any.
    pick = None

    @property
    def schedule(self) -> dict:
        """CC-79: what the planner needs to know when it's on: upcoming dates and times, the weekly pattern
        (the event's own, else its venue's show times), and the venue's opening hours."""
        today = datetime.now(_CHICAGO).date().isoformat()
        dates = sorted((p for p in self.performances or [] if (p.get("date") or "") >= today),
                       key=lambda p: (p.get("date"), p.get("time") or ""))[:120]
        own = bool(self.weekly)
        venue = self.venue
        return {"dates": dates, "weekly": self.weekly if own else (venue.show_times if venue else {}) or {},
                "weekly_from": "event" if own else ("venue" if venue and venue.show_times else ""),
                "hours": (venue.open_hours if venue else {}) or {}}


class Attended(Base):
    __tablename__ = "attended"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event: Mapped[Event] = relationship(lazy="joined")


class Plan(Base):
    """A user's own plan for an event (CC-53): interested, or going on a date. Private to them;
    sharing with friends comes later and must be opted into."""
    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("(status = 'going') = (going_on IS NOT NULL)", name="ck_plans_going_has_a_date"),
        Index("ix_plans_user_status", "user_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[PlanStatus] = mapped_column(_enum(PlanStatus, "plan_status"))
    going_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())

    event: Mapped[Event] = relationship(lazy="joined")


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
    page_state: Mapped[dict] = mapped_column(JSON, default=dict)  # per page: etag, last_modified, text hash
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(20), default="")  # ok | unchanged | blocked | error
    last_error: Mapped[str] = mapped_column(Text, default="")
    venue_hours: Mapped[dict] = mapped_column(JSON, default=dict)  # what the venue's site says, for CC-78
    venue: Mapped["Venue | None"] = relationship(lazy="joined")


class DiscoveredItem(Base):
    """An event a source listed: new to us, already in the catalog ("listed"), or a proposed update
    to a listed event. Admins approve, reject or dismiss it (CC-71)."""
    __tablename__ = "discovered_items"
    __table_args__ = (UniqueConstraint("source_id", "key", name="uq_discovered_source_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("discovery_sources.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(500))  # the event's own page, or its normalized title
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new | listed | update | approved | rejected | skipped
    skip_reason: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(2000), default="")
    kind: Mapped[str] = mapped_column(String(30), default="")  # production, exhibition, talk, ... as read
    category: Mapped[str | None] = mapped_column(String(40))
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
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped[DiscoverySource] = relationship(lazy="joined")


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


class DiscoveryCheck(Base):
    """One check of one source (or one event's page): what happened and what it cost. Feeds the
    sources list, the weekly recap (CC-77) and the monthly cost."""
    __tablename__ = "discovery_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("discovery_sources.id", ondelete="CASCADE"), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    what: Mapped[str] = mapped_column(String(20), default="source")  # source | detail (one event's page)
    status: Mapped[str] = mapped_column(String(20))  # ok | unchanged | blocked | error
    new_items: Mapped[int] = mapped_column(Integer, default=0)
    updates: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    pages_read: Mapped[int] = mapped_column(Integer, default=0)  # pages that had to go to the model
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
