from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from ..deps import DB, AdminUser
from ..models import Event, EventStatus
from ..schemas import EventAdminOut, EventAdminPage, EventInterestOut, StatsOut, TrafficOut
from ..photos import CHECK_USD, checks_this_month
from ..stats import event_interest, today_in_chicago, weekly_stats
from ..traffic import summary as traffic_summary
from ..visibility import utcnow

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/events", response_model=EventAdminPage)
def review_queue(db: DB, _: AdminUser,
                 status_: Annotated[EventStatus, Query(alias="status")] = EventStatus.pending,
                 limit: Annotated[int, Query(ge=1, le=500)] = 100,
                 offset: Annotated[int, Query(ge=0)] = 0):
    """Submissions to review, oldest first."""
    cond = Event.status == status_
    total = db.scalar(select(func.count(Event.id)).where(cond))
    rows = db.scalars(select(Event).where(cond).order_by(Event.created_at, Event.id)
                      .limit(limit).offset(offset)).all()
    return EventAdminPage(items=rows, total=total, limit=limit, offset=offset)


@router.get("/events/scheduled", response_model=list[EventAdminOut])
def scheduled(db: DB, _: AdminUser):
    """Approved events waiting for their publish time, soonest first."""
    return db.scalars(select(Event).where(Event.status == EventStatus.approved, Event.publish_at > utcnow())
                      .order_by(Event.publish_at, Event.id)).all()


def _set_status(db: DB, event_id: int, new: EventStatus) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    event.status = new
    event.publish_at = None  # approving publishes now, including a scheduled event
    db.commit()
    db.refresh(event)
    return event


@router.post("/events/{event_id}/approve", response_model=EventAdminOut)
def approve(event_id: int, db: DB, _: AdminUser):
    """Publish now: approves a pending event, or brings a scheduled one forward."""
    return _set_status(db, event_id, EventStatus.approved)


@router.post("/events/{event_id}/reject", response_model=EventAdminOut)
def reject(event_id: int, db: DB, _: AdminUser):
    return _set_status(db, event_id, EventStatus.rejected)


@router.get("/stats", response_model=StatsOut)
def stats(db: DB, _: AdminUser, weeks: Annotated[int, Query(ge=1, le=52)] = 12,
          today: Annotated[date | None, Query(description="The caller's local date")] = None):
    """Sign-ups and member activity for the last N weeks (CC-67). Totals only, never who."""
    out = weekly_stats(db, _callers_day(today), weeks)
    checks = checks_this_month(db)  # CC-76: Amazon Rekognition's cost this month
    return {**out, "photo_checks_month": checks, "photo_checks_usd": round(checks * CHECK_USD, 2)}


def _callers_day(today: date | None) -> date:
    server = today_in_chicago()
    return today if today is not None and abs((today - server).days) <= 1 else server


@router.get("/stats/events", response_model=list[EventInterestOut])
def stats_events(db: DB, _: AdminUser,
                 range_: Annotated[Literal["week", "30d", "all"], Query(alias="range")] = "30d",
                 include_closed: bool = False,
                 category: Annotated[list[str] | None, Query(description="Only these event types")] = None,
                 limit: Annotated[int, Query(ge=1, le=50)] = 15,
                 today: Annotated[date | None, Query(description="The caller's local date")] = None):
    """CC-69: the events drawing the most interest and commitments from members, with "I've been"
    counts. range: this week (from Monday), the last 30 days, or all time; category narrows it to event
    types. Totals only, never who."""
    day = _callers_day(today)
    since = {"week": day - timedelta(days=day.weekday()), "30d": day - timedelta(days=29), "all": None}[range_]
    return event_interest(db, day, since, include_closed, limit, category)


@router.get("/stats/traffic", response_model=TrafficOut)
def stats_traffic(db: DB, _: AdminUser, days: Annotated[int, Query(ge=1, le=366)] = 30,
                  today: Annotated[date | None, Query(description="The caller's local date")] = None):
    """CC-68: views, unique and returning visitors, ticket clicks and outings planned, day by day. Counts only."""
    return traffic_summary(db, _callers_day(today), days)
