from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import case, delete, select

from ..deps import DB, CurrentUser
from ..models import Attended, Event, Plan, PlanStatus
from ..picks import attach_picks
from ..schemas import AttendedOut, EventOut, PlanIn, PlanOut
from ..visibility import is_live, live_condition

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/attended", response_model=list[AttendedOut])
def list_attended(db: DB, user: CurrentUser):
    rows = db.scalars(select(Attended).where(Attended.user_id == user.id)
                      .order_by(Attended.created_at.desc())).all()
    attach_picks(db, [r.event for r in rows])
    return rows


@router.put("/attended/{event_id}", response_model=AttendedOut)
def mark_attended(event_id: int, db: DB, user: CurrentUser, response: Response):
    """Idempotent: marking an event twice is fine and returns the existing mark.
    Having been, any plan for the event is done with, so it's cleared (CC-63); un-marking doesn't bring it back."""
    mark = db.get(Attended, (user.id, event_id))
    if mark is None:
        event = db.get(Event, event_id)
        if event is None or not is_live(event):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
        mark = Attended(user_id=user.id, event_id=event_id)
        db.add(mark)
        response.status_code = status.HTTP_201_CREATED
    db.execute(delete(Plan).where(Plan.user_id == user.id, Plan.event_id == event_id))
    db.commit()
    db.refresh(mark)
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


# ---- plans: interested, or going on a date (CC-54) -------------------------------------
# Private to the user: nothing here is readable by anyone else, admins included. Sharing with
# friends (CC-60) will be opt-in and separate.

def _caller_today(today: date | None) -> date:
    """The caller's local date, if it's plausible (within a day of the server's), else the server's.
    A Chicago evening is already "tomorrow" in UTC, so the page sends its own date."""
    server = date.today()
    return today if today is not None and abs((today - server).days) <= 1 else server


@router.get("/plans", response_model=list[PlanOut])
def list_plans(db: DB, user: CurrentUser):
    """Your plans for live events: going first (soonest date first), then interested (newest first).
    Plans for withdrawn or unpublished events are left out (and come back if the event does)."""
    going_first = case((Plan.status == PlanStatus.going, 0), else_=1)
    rows = db.scalars(select(Plan).join(Event).where(Plan.user_id == user.id, live_condition())
                      .order_by(going_first, Plan.going_on, Plan.updated_at.desc(), Plan.event_id)).unique().all()
    attach_picks(db, [r.event for r in rows])
    return rows


@router.put("/plans/{event_id}", response_model=PlanOut)
def set_plan(event_id: int, body: PlanIn, db: DB, user: CurrentUser, response: Response,
             today: Annotated[date | None, Query(description="Your local date, for 'not in the past'")] = None):
    """Mark an event interested, or going on a date. Replaces any plan you already had for it.
    A going date must be a day the event is on (open runs: any day from today) and not in the past."""
    event = db.get(Event, event_id)
    if event is None or not is_live(event):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    if body.status == "going":
        day, first = body.date, _caller_today(today)
        if day < first:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That date has already passed")
        if (event.begin_date and day < event.begin_date) or (event.end_date and day > event.end_date):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The event isn't on that date")
        if day > first + timedelta(days=730):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a date in the next two years")
    plan = db.get(Plan, (user.id, event_id))
    if plan is None:
        plan = Plan(user_id=user.id, event_id=event_id)
        db.add(plan)
        response.status_code = status.HTTP_201_CREATED
    plan.status = PlanStatus(body.status)
    plan.going_on = body.date if body.status == "going" else None
    db.commit()
    db.refresh(plan)
    return plan


@router.delete("/plans/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def clear_plan(event_id: int, db: DB, user: CurrentUser):
    """Clear your plan for an event. Idempotent."""
    db.execute(delete(Plan).where(Plan.user_id == user.id, Plan.event_id == event_id))
    db.commit()
