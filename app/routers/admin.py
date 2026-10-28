import uuid
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from ..categories import CATEGORIES
from ..deps import DB, AdminUser
from ..discovery.rules import norm_title
from ..models import Attended, Event, EventStatus, SubmissionRule, Venue
from ..schemas import (EventAdminOut, EventAdminPage, EventInterestOut, StatsOut, SubmissionRuleIn, SubmissionRuleOut,
                       TakeHideIn, TrafficOut)
from ..photos import CHECK_USD, checks_this_month
from ..stats import event_interest, today_in_chicago, weekly_stats
from ..traffic import summary as traffic_summary
from ..visibility import utcnow

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/events", response_model=EventAdminPage)
def review_queue(db: DB, _: AdminUser,
                 status_: Annotated[EventStatus, Query(alias="status")] = EventStatus.pending,
                 limit: Annotated[int, Query(ge=1, le=500)] = 100,
                 offset: Annotated[int, Query(ge=0)] = 0,
                 caught: Annotated[bool, Query(description="Only those an auto-reject rule caught, newest first")] = False):
    """Submissions to review, oldest first."""
    cond = Event.status == status_
    order = (Event.created_at, Event.id)
    if caught:  # CC-100
        cond, order = cond & (Event.reject_reason != ""), (Event.created_at.desc(), Event.id.desc())
    total = db.scalar(select(func.count(Event.id)).where(cond))
    rows = db.scalars(select(Event).where(cond).order_by(*order)
                      .limit(limit).offset(offset)).all()
    return EventAdminPage(items=rows, total=total, limit=limit, offset=offset)


@router.get("/events/other-types", response_model=list[EventAdminOut])
def other_types(db: DB, _: AdminUser):
    """CC-125: events whose category isn't one of the five (the migration didn't guess), for an admin to fix."""
    return db.scalars(select(Event).where(Event.category.not_in(CATEGORIES)).order_by(Event.title, Event.id)).all()


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
    event.reject_reason = ""  # CC-100: an admin decided, so it's no longer one a rule caught
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


# ---- auto-reject rules for member submissions (CC-100) ----

def _rule_out(r: SubmissionRule) -> SubmissionRuleOut:
    venue = r.venue and (r.venue.name + (f" · {r.venue.neighborhood}" if r.venue.neighborhood else ""))
    return SubmissionRuleOut(id=r.id, kind=r.kind, phrase=r.phrase, venue_id=r.venue_id, venue_name=venue or None,
                             caught=r.caught, created_at=r.created_at)


@router.get("/submission-rules", response_model=list[SubmissionRuleOut])
def submission_rules(db: DB, _: AdminUser):
    """The auto-reject rules, newest first, with how many submissions each has caught."""
    return [_rule_out(r) for r in db.scalars(select(SubmissionRule).order_by(SubmissionRule.id.desc()))]


@router.post("/submission-rules", response_model=SubmissionRuleOut, status_code=status.HTTP_201_CREATED)
def add_submission_rule(body: SubmissionRuleIn, db: DB, _: AdminUser):
    if body.kind == "venue":
        if body.venue_id is None or db.get(Venue, body.venue_id) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a venue from the list")
        rule = SubmissionRule(kind="venue", venue_id=body.venue_id)
        same = SubmissionRule.venue_id == body.venue_id
    else:
        phrase = norm_title(body.phrase)  # case and punctuation don't matter, as with the skip rules
        if not phrase:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Give a word or phrase from the event name")
        rule = SubmissionRule(kind="name", phrase=phrase)
        same = SubmissionRule.phrase == phrase
    if db.scalar(select(SubmissionRule.id).where(SubmissionRule.kind == body.kind, same)):
        raise HTTPException(status.HTTP_409_CONFLICT, "There's already a rule for that")
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


@router.delete("/submission-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_submission_rule(rule_id: int, db: DB, _: AdminUser):
    """Stop rejecting. Submissions it already caught stay rejected (they can still be approved)."""
    rule = db.get(SubmissionRule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    db.delete(rule)
    db.commit()


@router.put("/takes/{event_id}/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def hide_take(event_id: int, user_id: uuid.UUID, body: TakeHideIn, db: DB, _: AdminUser):
    """CC-112: hide a member's public take (moderation), or unhide it. The journal entry itself is kept, and a
    hidden take stays hidden whatever the member edits, until an admin unhides it."""
    mark = db.get(Attended, (user_id, event_id))
    if mark is None or mark.shared_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Take not found")
    mark.hidden = body.hidden
    db.commit()


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
