from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import case, delete, select

from ..deps import DB, CurrentUser
from ..models import Attended, Event, Plan, PlanStatus
from ..crewplans import attach_crew_plans, my_crew_ids, share_plan, shared_with, unshare
from ..picks import attach_picks
from ..schemas import AttendedOut, EventOut, PlanCrewsIn, PlanIn, PlanOut
from ..visibility import is_live, live_condition

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/attended", response_model=list[AttendedOut])
def list_attended(db: DB, user: CurrentUser):
    rows = db.scalars(select(Attended).where(Attended.user_id == user.id)
                      .order_by(Attended.created_at.desc())).all()
    attach_picks(db, [r.event for r in rows])
    attach_crew_plans(db, [r.event for r in rows], user)
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
    unshare(db, user.id, event_id=event_id)  # CC-75: the plan's gone, so it's no longer shared
    db.execute(delete(Plan).where(Plan.user_id == user.id, Plan.event_id == event_id))
    db.commit()
    db.refresh(mark)
    attach_crew_plans(db, [mark.event], user)
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
    attach_crew_plans(db, [r.event for r in rows], user)
    shares = shared_with(db, user, {r.event_id for r in rows})
    for r in rows:
        r.crews = shares.get(r.event_id, [])
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
    return _plan_out(db, plan, user)


def _plan_out(db, plan: Plan, user) -> Plan:
    attach_picks(db, [plan.event])
    attach_crew_plans(db, [plan.event], user)
    plan.crews = shared_with(db, user, {plan.event_id}).get(plan.event_id, [])
    return plan


@router.put("/plans/{event_id}/crews", response_model=PlanOut)
def share_with_crews(event_id: int, body: PlanCrewsIn, db: DB, user: CurrentUser):
    """CC-75: share your plan for this event with exactly these crews of yours ([] = nobody). Only members
    of those crews see it. Changing the plan later keeps the crews; clearing it unshares it."""
    plan = db.get(Plan, (user.id, event_id))
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mark it Interested or choose a date first")
    if set(body.crews) - my_crew_ids(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Crew not found")
    share_plan(db, user, event_id, body.crews)
    db.commit()
    return _plan_out(db, plan, user)


@router.delete("/plans/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def clear_plan(event_id: int, db: DB, user: CurrentUser):
    """Clear your plan for an event, and its sharing with crews. Idempotent."""
    unshare(db, user.id, event_id=event_id)
    db.execute(delete(Plan).where(Plan.user_id == user.id, Plan.event_id == event_id))
    db.commit()
