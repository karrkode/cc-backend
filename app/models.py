"""Database tables. Alembic migrations in alembic/versions must match these."""
import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint, Uuid, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Role(str, enum.Enum):
    user = "user"
    admin = "admin"


class Area(str, enum.Enum):
    city = "city"
    suburbs = "suburbs"
    out_of_town = "out_of_town"


class EventStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    withdrawn = "withdrawn"  # was on the Google Sheet, then removed from it; hidden but kept


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex; the raw token is never stored
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())

    venue: Mapped[Venue] = relationship(lazy="joined")
    submitter: Mapped[User | None] = relationship()  # loaded only where admins need it


class Attended(Base):
    __tablename__ = "attended"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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
