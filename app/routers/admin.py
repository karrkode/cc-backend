from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from ..deps import DB, AdminUser
from ..models import Event, EventStatus, SheetSync
from ..schemas import EventAdminOut, EventAdminPage, SheetSyncOut, StatsOut
from ..stats import today_in_chicago, weekly_stats
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


@router.get("/syncs", response_model=list[SheetSyncOut])
def sync_runs(db: DB, _: AdminUser, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """The latest Google Sheet sync runs, newest first: what changed, or why a run failed."""
    return db.scalars(select(SheetSync).order_by(SheetSync.started_at.desc(), SheetSync.id.desc()).limit(limit)).all()


@router.get("/stats", response_model=StatsOut)
def stats(db: DB, _: AdminUser, weeks: Annotated[int, Query(ge=1, le=52)] = 12,
          today: Annotated[date | None, Query(description="The caller's local date")] = None):
    """Sign-ups and member activity for the last N weeks (CC-67). Totals only, never who."""
    server = today_in_chicago()
    day = today if today is not None and abs((today - server).days) <= 1 else server
    return weekly_stats(db, day, weeks)
