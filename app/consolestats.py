"""CC-240: one Monday-Sunday week of numbers for the owner's Growth Console. Counts only, never a name, email or id.

A week runs from Monday 00:00 to the next Monday 00:00, Chicago time. A number the site can't count is None
(the console shows it as "not measured"), and zero means it counted and found none.
"""
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from .clock import CHICAGO, as_utc
from .emailcaps import INVITE
from .models import (AppSetting, Crew, CrewInvite, CrewMember, Plan, PlanShare, SentEmail, TrafficCount,
                     TrafficVisitorDay, User)

PLAN_SHARES_MARKER = "plan_shares_timed_since"  # set by migration 0058: the moment shares started getting a date


def last_finished_monday(today: date) -> date:
    """The Monday of the most recent week that has fully ended."""
    return today - timedelta(days=today.weekday() + 7)


def check_week(week: date, today: date) -> str | None:
    """Why this week can't be asked for, or None."""
    if week.weekday() != 0:
        return "week must be a Monday"
    if week + timedelta(days=7) > today:
        return "that week hasn't ended yet"
    return None


def _bounds(week: date) -> tuple[datetime, datetime]:
    """The week's start and end as UTC instants (a week with a daylight-saving change is 167 or 169 hours)."""
    start = datetime(week.year, week.month, week.day, tzinfo=CHICAGO).astimezone(UTC)
    end_day = week + timedelta(days=7)
    return start, datetime(end_day.year, end_day.month, end_day.day, tzinfo=CHICAGO).astimezone(UTC)


def _count(db, column, start, end, *where) -> int:
    return db.scalar(select(func.count()).select_from(column.class_).where(column >= start, column < end, *where)) or 0


def week_numbers(db, week: date) -> dict:
    start, end = _bounds(week)
    last = week + timedelta(days=6)

    # Visits: a visitor on a day (the cookie-free counter's unique visitors, added up over the days). Null for a
    # week before the counter's first day: those weeks weren't measured, which isn't the same as no visitors.
    since = db.scalar(select(func.min(TrafficCount.day)))
    visits = None
    if since is not None and since <= week:
        visits = db.scalar(select(func.count()).select_from(TrafficVisitorDay)
                           .where(TrafficVisitorDay.day >= week, TrafficVisitorDay.day <= last)) or 0

    # Marks that still exist: a plan cleared later, or turned into "I've been", has left no row to count.
    saves = _count(db, Plan.created_at, start, end)

    # Invites: links made, plus invite emails sent (sent_emails is never pruned, unlike the invites themselves).
    invites = _count(db, CrewInvite.created_at, start, end) + _count(db, SentEmail.sent_at, start, end,
                                                                     SentEmail.kind == INVITE)

    # Joins: everyone who entered a crew other than by starting it. That is an accepted email invite or a link.
    joins = db.scalar(select(func.count()).select_from(CrewMember).join(Crew, Crew.id == CrewMember.crew_id)
                      .where(CrewMember.joined_at >= start, CrewMember.joined_at < end,
                             CrewMember.user_id != Crew.owner_id)) or 0

    # Plans shared with a crew: only weeks after shares began to be dated (null for earlier ones).
    marker = db.get(AppSetting, PLAN_SHARES_MARKER)
    stamp = (marker.value or {}).get("at") if marker else None
    suggestions = None
    if stamp and start >= as_utc(datetime.fromisoformat(stamp.replace("Z", "+00:00"))):
        suggestions = db.scalar(select(func.count()).select_from(
            select(PlanShare.user_id, PlanShare.event_id)
            .where(PlanShare.created_at >= start, PlanShare.created_at < end).distinct().subquery())) or 0

    return {"week_start": week.isoformat(), "week_end": last.isoformat(), "site_visits": visits,
            "new_members": _count(db, User.created_at, start, end), "event_saves": saves, "crew_invites": invites,
            "crew_invite_accepts": joins, "crew_suggestions": suggestions}

