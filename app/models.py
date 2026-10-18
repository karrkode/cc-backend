"""Database tables. Alembic migrations in alembic/versions must match these."""
import enum
import re
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint, Uuid, false, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None


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


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("end_date IS NULL OR begin_date IS NULL OR end_date >= begin_date",
                        name="ck_events_dates_in_order"),
        CheckConstraint("min_price IS NULL OR min_price >= 0", name="ck_events_min_price_not_negative"),
        Index("ix_events_status_end_date", "status", "end_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str | None] = mapped_column(String(40), unique=True)  # id from the old sheet, for re-seeding
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
    """One run of the nightly Google Sheet sync, for checking what changed and when."""
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
