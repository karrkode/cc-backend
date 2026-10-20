"""The admin Stats page (CC-67): sign-ups and member activity, week by week.

Weeks run Monday to Sunday in Chicago time. Admin accounts are left out: these numbers are about
members. Only totals leave this module; never who.
"""
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from .models import ActiveDay, Attended, Event, Plan, PlanStatus, Role, User
from .visibility import live_condition

CHICAGO = ZoneInfo("America/Chicago")
RETURN_AFTER = timedelta(days=28)  # "still active 4 weeks after joining"


def chicago_day(ts: datetime) -> date:
    if ts.tzinfo is None:  # SQLite hands back naive UTC
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(CHICAGO).date()


def today_in_chicago() -> date:
    return datetime.now(CHICAGO).date()


def record_active(db, user) -> None:
    """Note that this member used the site today (at most one row a day). The caller commits."""
    day = today_in_chicago()
    if db.get(ActiveDay, (user.id, day)) is not None:
        return
    try:
        with db.begin_nested():  # two tabs refreshing at once: the second insert is a duplicate
            db.add(ActiveDay(user_id=user.id, day=day))
    except IntegrityError:
        pass


def weekly_stats(db, today: date, weeks: int = 12) -> dict:
    this_monday = today - timedelta(days=today.weekday())
    starts = [this_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    first = starts[0]

    def week_of(day: date):
        if day < first or day > today:
            return None
        return (day - first).days // 7

    rows = [{"start": s, "registrations": 0, "confirmed": 0, "active": 0, "plans_interested": 0,
             "plans_going": 0, "attended": 0, "submitted": 0} for s in starts]
    members = {u.id: u for u in db.scalars(select(User).where(User.role == Role.user))}

    for u in members.values():
        w = week_of(chicago_day(u.created_at))
        if w is not None:
            rows[w]["registrations"] += 1
            rows[w]["confirmed"] += u.email_verified_at is not None

    active_weeks: dict[int, set] = {}
    first_seen = None
    for user_id, day in db.execute(select(ActiveDay.user_id, ActiveDay.day)):
        if user_id not in members:
            continue
        first_seen = day if first_seen is None or day < first_seen else first_seen
        w = week_of(day)
        if w is not None:
            active_weeks.setdefault(w, set()).add(user_id)
    for w, ids in active_weeks.items():
        rows[w]["active"] = len(ids)

    for user_id, status, created in db.execute(select(Plan.user_id, Plan.status, Plan.created_at)):
        w = user_id in members and week_of(chicago_day(created))
        if w is not None and w is not False:
            rows[w]["plans_going" if status == PlanStatus.going else "plans_interested"] += 1
    for user_id, created in db.execute(select(Attended.user_id, Attended.created_at)):
        w = user_id in members and week_of(chicago_day(created))
        if w is not None and w is not False:
            rows[w]["attended"] += 1
    for user_id, created in db.execute(select(Event.submitted_by, Event.created_at).where(Event.submitted_by.is_not(None))):
        w = user_id in members and week_of(chicago_day(created))
        if w is not None and w is not False:
            rows[w]["submitted"] += 1

    # Retention: of the members who joined at least 4 weeks ago, how many used the site again
    # 4 weeks or more after joining. Only counts members who joined after activity tracking began.
    days_by_user: dict = {}
    for user_id, day in db.execute(select(ActiveDay.user_id, ActiveDay.day)):
        days_by_user.setdefault(user_id, []).append(day)
    eligible = retained = 0
    for u in members.values():
        joined = chicago_day(u.created_at)
        if first_seen is None or joined < first_seen or joined + RETURN_AFTER > today:
            continue
        eligible += 1
        retained += any(d >= joined + RETURN_AFTER for d in days_by_user.get(u.id, []))

    return {
        "today": today, "weeks": rows,
        "members": len(members), "confirmed_members": sum(u.email_verified_at is not None for u in members.values()),
        "active_since": first_seen, "retention": {"eligible": eligible, "retained": retained},
    }


def event_interest(db, today: date, since: date | None, include_closed: bool, limit: int) -> list[dict]:
    """CC-69: live events ranked by members' interest and commitments, with "I've been" counts.
    Counts plans and marks made on or after `since` (Chicago time; None = all time), by members only
    (admins are left out). Current plans are counted: a plan changed from interested to going counts
    as going. Totals only, never who."""
    members = set(db.scalars(select(User.id).where(User.role == Role.user)))

    def recent(ts) -> bool:
        return since is None or chicago_day(ts) >= since

    counts: dict[int, dict] = {}
    for user_id, event_id, status, created in db.execute(select(Plan.user_id, Plan.event_id, Plan.status, Plan.created_at)):
        if user_id in members and recent(created):
            c = counts.setdefault(event_id, {"interested": 0, "going": 0, "attended": 0})
            c["going" if status == PlanStatus.going else "interested"] += 1
    for user_id, event_id, created in db.execute(select(Attended.user_id, Attended.event_id, Attended.created_at)):
        if user_id in members and recent(created):
            counts.setdefault(event_id, {"interested": 0, "going": 0, "attended": 0})["attended"] += 1
    if not counts:
        return []
    conds = [live_condition(), Event.id.in_(counts)]
    if not include_closed:
        conds.append(or_(Event.end_date.is_(None), Event.end_date >= today))
    events = db.scalars(select(Event).where(*conds)).all()
    rows = [{"event": e, **counts[e.id]} for e in events]
    rows.sort(key=lambda r: (-(r["interested"] + r["going"]), -r["going"], -r["attended"], r["event"].title.lower()))
    return rows[:limit]
