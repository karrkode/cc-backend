"""Cookie-free traffic counting (CC-68).

The site sends a small ping for each view it shows, each click out to an event's ticket page, and
each outing it plans. We keep daily totals, plus which visitors came on which day so we can count
unique and returning visitors.

A visitor is known only by a one-way hash of a random ID their browser keeps (localStorage, never a
cookie). The site says so in its footer and lets people turn it off; it also sends nothing when the
browser asks not to be tracked (Global Privacy Control). Pings without an ID still count the view,
and count the visitor for that day only, by a hash of their address and browser that changes daily
and can't be turned back into either. No address, ID or browser string is ever stored.

CC-110: a ping can also say where this visit came from: a tagged link's utm_source / utm_medium /
utm_campaign, or else just the domain of the site that linked here (never the full address). We keep
which visitors came from each source on a day, and ticket clicks by source.
"""
import hashlib
import re
from datetime import date, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .models import TrafficCount, TrafficSourceCount, TrafficSourceVisit, TrafficVisitor, TrafficVisitorDay

VIEWS = ("home", "catalog", "plan", "submit", "account", "review", "venues", "stats", "reset", "verify")
KINDS = ("view", "ticket", "outing")
BOTS = re.compile(r"bot|crawl|spider|slurp|preview|headless|lighthouse|monitor|curl|wget|python|httpclient", re.I)


TAG_JUNK = re.compile(r"[^a-z0-9._+-]+")
DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
OWN_SITES = ("chiculturecrew.com", "localhost", "127.0.0.1")
SHIMS = ("www.", "m.", "l.", "lm.", "mobile.")  # link wrappers and mobile hosts: l.facebook.com is facebook.com
GOOGLE = re.compile(r"^google(\.[a-z]{2,3}){1,2}$")  # google.co.uk, google.de: all one search engine


def _tag(value: str | None, limit: int) -> str:
    return TAG_JUNK.sub("-", (value or "").strip().lower()).strip("-")[:limit]


def source_of(utm_source: str | None, utm_medium: str | None, utm_campaign: str | None,
              ref: str | None) -> tuple[str, str, str] | None:
    """(source, medium, campaign) for a visit, or None for direct / unknown. A tagged link wins over the
    referring site; a referring site is kept as its bare domain, with medium "site". Our own site isn't a source."""
    source = _tag(utm_source, 60)
    if source:
        return source, _tag(utm_medium, 40), _tag(utm_campaign, 60)
    host = (ref or "").strip().lower().rstrip(".")
    if not DOMAIN.match(host) or len(host) > 253:
        return None
    for shim in SHIMS:
        if host.startswith(shim) and host.count(".") > 1:
            host = host[len(shim):]
    if any(host == own or host.endswith("." + own) for own in OWN_SITES):
        return None
    if GOOGLE.match(host):
        host = "google.com"
    return host[:60], "site", ""


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


def record(db, day: date, kind: str, name: str, visitor: str, lasting: bool,
           source: tuple[str, str, str] | None = None) -> None:
    """Count one ping, and where the visit came from if known (CC-110). The caller commits."""
    if source is not None:
        _record_source(db, day, kind, visitor, source)
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


def _record_source(db, day: date, kind: str, visitor: str, source: tuple[str, str, str]) -> None:
    src, medium, campaign = source
    if db.get(TrafficSourceVisit, (day, visitor, src, medium, campaign)) is None:
        try:
            with db.begin_nested():
                db.add(TrafficSourceVisit(day=day, visitor=visitor, source=src, medium=medium, campaign=campaign))
        except IntegrityError:
            pass
    if kind != "ticket":
        return
    where = (TrafficSourceCount.day == day, TrafficSourceCount.source == src,
             TrafficSourceCount.medium == medium, TrafficSourceCount.campaign == campaign)
    if db.execute(update(TrafficSourceCount).where(*where).values(tickets=TrafficSourceCount.tickets + 1)).rowcount:
        return
    try:
        with db.begin_nested():
            db.add(TrafficSourceCount(day=day, source=src, medium=medium, campaign=campaign, tickets=1))
    except IntegrityError:  # someone else made the row first
        db.execute(update(TrafficSourceCount).where(*where).values(tickets=TrafficSourceCount.tickets + 1))


def _sources(db, first: date, today: date, period_visitors: set, tickets: int) -> tuple[list[dict], dict]:
    """CC-110: visitors and ticket clicks by source over the period, most visitors first, and what's left
    over as direct / unknown."""
    rows: dict[tuple, dict] = {}

    def row(key):
        return rows.setdefault(key, {"source": key[0], "medium": key[1], "campaign": key[2], "visitors": set(), "tickets": 0})
    for src, medium, campaign, visitor in db.execute(
            select(TrafficSourceVisit.source, TrafficSourceVisit.medium, TrafficSourceVisit.campaign, TrafficSourceVisit.visitor)
            .where(TrafficSourceVisit.day >= first, TrafficSourceVisit.day <= today)):
        row((src, medium, campaign))["visitors"].add(visitor)
    for src, medium, campaign, count in db.execute(
            select(TrafficSourceCount.source, TrafficSourceCount.medium, TrafficSourceCount.campaign, TrafficSourceCount.tickets)
            .where(TrafficSourceCount.day >= first, TrafficSourceCount.day <= today)):
        row((src, medium, campaign))["tickets"] += count
    sourced = set().union(*(r["visitors"] for r in rows.values())) if rows else set()
    out = [{**r, "visitors": len(r["visitors"])} for r in rows.values()]
    out.sort(key=lambda r: (-r["visitors"], -r["tickets"], r["source"], r["medium"], r["campaign"]))
    direct = {"visitors": len(period_visitors - sourced),
              "tickets": max(0, tickets - sum(r["tickets"] for r in out))}
    return out, direct


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
    sources, direct = _sources(db, first, today, period_visitors, sum(r["tickets"] for r in rows.values()))
    return {"days": list(rows.values()), "by_view": dict(sorted(by_view.items(), key=lambda kv: -kv[1])),
            "visitors": len(period_visitors), "returning_visitors": len(period_returning), "counting_since": since,
            "sources": sources, "direct": direct}
