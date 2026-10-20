"""Cookie-free traffic counting (CC-68).

The site sends a small ping for each view it shows, each click out to an event's ticket page, and
each outing it plans. We keep daily totals, plus which visitors came on which day so we can count
unique and returning visitors.

A visitor is known only by a one-way hash of a random ID their browser keeps (localStorage, never a
cookie). The site says so in its footer and lets people turn it off; it also sends nothing when the
browser asks not to be tracked (Global Privacy Control). Pings without an ID still count the view,
and count the visitor for that day only, by a hash of their address and browser that changes daily
and can't be turned back into either. No address, ID or browser string is ever stored.
"""
import hashlib
import re
from datetime import date, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .models import TrafficCount, TrafficVisitor, TrafficVisitorDay

VIEWS = ("home", "catalog", "plan", "submit", "account", "review", "venues", "stats", "reset", "verify")
KINDS = ("view", "ticket", "outing")
BOTS = re.compile(r"bot|crawl|spider|slurp|preview|headless|lighthouse|monitor|curl|wget|python|httpclient", re.I)


def is_bot(user_agent: str) -> bool:
    return not user_agent or bool(BOTS.search(user_agent))


def visitor_key(vid: str | None, day: date, address: str, user_agent: str) -> tuple[str, bool]:
    """(hash, lasting): lasting is False for the day-only stand-in used when there's no browser ID."""
    secret = get_settings().jwt_secret
    if vid:
        return hashlib.sha256(f"{secret}|vid|{vid}".encode()).hexdigest()[:40], True
    return "d:" + hashlib.sha256(f"{secret}|day|{day}|{address}|{user_agent}".encode()).hexdigest()[:38], False


def _bump(db, day: date, kind: str, name: str) -> None:
    done = db.execute(update(TrafficCount).where(TrafficCount.day == day, TrafficCount.kind == kind,
                                                 TrafficCount.name == name).values(count=TrafficCount.count + 1)).rowcount
    if done:
        return
    try:
        with db.begin_nested():
            db.add(TrafficCount(day=day, kind=kind, name=name, count=1))
    except IntegrityError:  # someone else made the row first
        db.execute(update(TrafficCount).where(TrafficCount.day == day, TrafficCount.kind == kind,
                                              TrafficCount.name == name).values(count=TrafficCount.count + 1))


def record(db, day: date, kind: str, name: str, visitor: str, lasting: bool) -> None:
    """Count one ping. The caller commits."""
    _bump(db, day, kind, name if kind == "view" else "")
    if db.get(TrafficVisitorDay, (day, visitor)) is None:
        try:
            with db.begin_nested():
                db.add(TrafficVisitorDay(day=day, visitor=visitor))
        except IntegrityError:
            pass
    if lasting and db.get(TrafficVisitor, visitor) is None:
        try:
            with db.begin_nested():
                db.add(TrafficVisitor(visitor=visitor, first_day=day))
        except IntegrityError:
            pass


def summary(db, today: date, days: int) -> dict:
    first = today - timedelta(days=days - 1)
    rows = {first + timedelta(days=i): {"day": first + timedelta(days=i), "views": 0, "visitors": 0, "returning": 0,
                                        "tickets": 0, "outings": 0} for i in range(days)}
    by_view: dict[str, int] = {}
    for day, kind, name, count in db.execute(select(TrafficCount.day, TrafficCount.kind, TrafficCount.name, TrafficCount.count)
                                             .where(TrafficCount.day >= first, TrafficCount.day <= today)):
        r = rows[day]
        if kind == "view":
            r["views"] += count
            by_view[name] = by_view.get(name, 0) + count
        elif kind == "ticket":
            r["tickets"] += count
        elif kind == "outing":
            r["outings"] += count
    seen = (select(TrafficVisitorDay.day, TrafficVisitorDay.visitor, TrafficVisitor.first_day)
            .outerjoin(TrafficVisitor, TrafficVisitor.visitor == TrafficVisitorDay.visitor)
            .where(TrafficVisitorDay.day >= first, TrafficVisitorDay.day <= today))
    period_visitors, period_returning = set(), set()
    for day, visitor, first_day in db.execute(seen):
        r = rows[day]
        r["visitors"] += 1
        period_visitors.add(visitor)
        if first_day is not None and first_day < day:
            r["returning"] += 1
            period_returning.add(visitor)
    since = db.scalar(select(func.min(TrafficCount.day)))
    return {"days": list(rows.values()), "by_view": dict(sorted(by_view.items(), key=lambda kv: -kv[1])),
            "visitors": len(period_visitors), "returning_visitors": len(period_returning), "counting_since": since}
