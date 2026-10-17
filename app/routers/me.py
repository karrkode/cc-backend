from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import delete, select

from ..deps import DB, CurrentUser
from ..models import Attended, Event, EventStatus
from ..schemas import AttendedOut, EventOut

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/attended", response_model=list[AttendedOut])
def list_attended(db: DB, user: CurrentUser):
    return db.scalars(select(Attended).where(Attended.user_id == user.id)
                      .order_by(Attended.created_at.desc())).all()


@router.put("/attended/{event_id}", response_model=AttendedOut)
def mark_attended(event_id: int, db: DB, user: CurrentUser, response: Response):
    """Idempotent: marking an event twice is fine and returns the existing mark."""
    existing = db.get(Attended, (user.id, event_id))
    if existing:
        return existing
    event = db.get(Event, event_id)
    if event is None or event.status != EventStatus.approved:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    mark = Attended(user_id=user.id, event_id=event_id)
    db.add(mark)
    db.commit()
    db.refresh(mark)
    response.status_code = status.HTTP_201_CREATED
    return mark


@router.delete("/attended/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def unmark_attended(event_id: int, db: DB, user: CurrentUser):
    db.execute(delete(Attended).where(Attended.user_id == user.id, Attended.event_id == event_id))
    db.commit()


@router.get("/submissions", response_model=list[EventOut])
def my_submissions(db: DB, user: CurrentUser):
    """Events this user submitted, with their review status."""
    return db.scalars(select(Event).where(Event.submitted_by == user.id)
                      .order_by(Event.created_at.desc(), Event.id.desc())).all()
