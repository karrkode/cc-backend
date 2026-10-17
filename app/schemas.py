"""Request and response shapes. FastAPI validates against these and builds /docs from them."""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from .models import Area, EventStatus, Role

Category = Literal["Theater", "Concert", "Comedy", "Visual Art"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth ------------------------------------------------------------------

class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    display_name: str = Field(default="", max_length=80)

    @field_validator("password")
    @classmethod
    def _bcrypt_limit(cls, v: str) -> str:
        if len(v.encode()) > 72:  # bcrypt only uses the first 72 bytes
            raise ValueError("Password must be at most 72 bytes")
        return v


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


class UserOut(ORM):
    id: uuid.UUID
    email: str
    display_name: str
    role: Role
    created_at: datetime


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


# ---- events ----------------------------------------------------------------

class EventOut(ORM):
    id: int
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


class EventPage(BaseModel):
    items: list[EventOut]
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
    """Give venue_id for a known venue, or venue (name + neighborhood) to use or add one."""
    venue_id: int | None = None
    venue: VenueIn | None = None

    @model_validator(mode="after")
    def _one_venue(self):
        if (self.venue_id is None) == (self.venue is None):
            raise ValueError("Give exactly one of venue_id or venue")
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


# ---- attended --------------------------------------------------------------

class AttendedOut(ORM):
    event: EventOut
    created_at: datetime
