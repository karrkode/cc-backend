from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from ..deps import DB, AdminUser
from ..models import Event, EventStatus
from ..schemas import EventOut, EventPage

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/events", response_model=EventPage)
def review_queue(db: DB, _: AdminUser,
                 status_: Annotated[EventStatus, Query(alias="status")] = EventStatus.pending,
                 limit: Annotated[int, Query(ge=1, le=500)] = 100,
                 offset: Annotated[int, Query(ge=0)] = 0):
    """Submissions to review, oldest first."""
    cond = Event.status == status_
    total = db.scalar(select(func.count(Event.id)).where(cond))
    rows = db.scalars(select(Event).where(cond).order_by(Event.created_at, Event.id)
                      .limit(limit).offset(offset)).all()
    return EventPage(items=rows, total=total, limit=limit, offset=offset)


def _set_status(db: DB, event_id: int, new: EventStatus) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    event.status = new
    db.commit()
    db.refresh(event)
    return event


@router.post("/events/{event_id}/approve", response_model=EventOut)
def approve(event_id: int, db: DB, _: AdminUser):
    return _set_status(db, event_id, EventStatus.approved)


@router.post("/events/{event_id}/reject", response_model=EventOut)
def reject(event_id: int, db: DB, _: AdminUser):
    return _set_status(db, event_id, EventStatus.rejected)
