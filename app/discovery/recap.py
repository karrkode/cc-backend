"""The weekly discovery recap (CC-77): one plain-text email, Tuesdays at 10 AM Chicago time, to
settings.recap_to. It covers the past seven days:

- what's waiting for review, with a link to Found events
- new events found, by source
- proposed updates waiting (prices, dates)
- what was left out automatically: by the admin's skip rules, and by the built-in rules
- sources that couldn't be read, with the reason, to check by hand
- how many checks ran and their estimated cost

It's sent even when the week was quiet ("Nothing new this week"), so a missing email means something
is wrong.
"""
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from ..config import get_settings
from ..mail import Email
from ..models import DiscoveredItem, DiscoveryCheck, DiscoverySource
from ..stats import CHICAGO
from .budget import budget_state
from .readers import cost_usd

DAYS = 7
CHANGE_NAMES = {"cost_text": "price", "min_price": "lowest price", "begin_date": "opens", "end_date": "closes"}


def _day(d) -> str:
    return f"{d:%a} {d:%b} {d.day}"


def _change(field: str, v) -> str:
    if v in (None, ""):
        return "none"
    if field in ("begin_date", "end_date"):
        d = datetime.fromisoformat(v)
        return f"{d:%b} {d.day}"
    if field == "min_price":
        return "free" if v == 0 else f"${v:g}"
    return str(v)


def _rows(pairs: list[tuple[str, int]], indent: str = "  ") -> list[str]:
    width = max((len(name) for name, _ in pairs), default=0)
    return [f"{indent}{name.ljust(width)}  {n}" for name, n in pairs]


def build_recap(db, now: datetime | None = None) -> Email:
    s = get_settings()
    now = now or datetime.now(UTC)
    since = now - timedelta(days=DAYS)
    first, last = since.astimezone(CHICAGO).date(), (now.astimezone(CHICAGO) - timedelta(days=1)).date()
    names = dict(db.execute(select(DiscoverySource.id, DiscoverySource.name)).all())

    week = db.scalars(select(DiscoveredItem).where(DiscoveredItem.first_seen_at >= since)).all()
    by_rule = [i for i in week if i.status == "rejected" and i.skip_reason.startswith("skip rule")]
    found = [i for i in week if i.status in ("new", "approved", "rejected") and i not in by_rule]
    built_in = [i for i in week if i.status == "skipped"]
    updates = db.scalars(select(DiscoveredItem).where(DiscoveredItem.status == "update")
                         .order_by(DiscoveredItem.first_seen_at)).all()
    waiting = dict(db.execute(select(DiscoveredItem.status, func.count()).where(DiscoveredItem.status.in_(["new", "update"]))
                              .group_by(DiscoveredItem.status)).all())
    unreadable = db.scalars(select(DiscoverySource).where(DiscoverySource.active, DiscoverySource.last_status.in_(["blocked", "error"]))
                            .order_by(func.lower(DiscoverySource.name))).all()
    checks, pages, tin, tout = db.execute(select(func.count(), func.coalesce(func.sum(DiscoveryCheck.pages_read), 0),
                                                 func.coalesce(func.sum(DiscoveryCheck.input_tokens), 0),
                                                 func.coalesce(func.sum(DiscoveryCheck.output_tokens), 0))
                                          .where(DiscoveryCheck.checked_at >= since)).one()

    new_n, upd_n = waiting.get("new", 0), waiting.get("update", 0)
    state, spent, budget = budget_state(db, now)
    quiet = not (found or updates or built_in or by_rule or unreadable) and state == "ok"
    lines = ["Hi,", "", f"Event discovery for {_day(first)} to {_day(last)}.", ""]
    lines += [f"Waiting for review: {new_n + upd_n} ({new_n} new {'event' if new_n == 1 else 'events'}, "
              f"{upd_n} proposed {'update' if upd_n == 1 else 'updates'})",
              f"Review them: {s.site_url.rstrip('/')}/#found", ""]
    if quiet:
        lines += ["Nothing new this week: no events found, nothing left out, and every source could be read.", ""]

    lines.append(f"NEW EVENTS FOUND THIS WEEK: {len(found)}")
    lines += _rows(Counter(names.get(i.source_id, "?") for i in found).most_common()) or ["  None."]
    lines.append("")

    lines.append(f"PROPOSED UPDATES WAITING: {len(updates)}")
    for i in updates[:20]:
        what = "; ".join(f"{CHANGE_NAMES.get(k, k)} {_change(k, v[0])} -> {_change(k, v[1])}"
                         for k, v in (i.changes or {}).items() if not (k == "min_price" and "cost_text" in i.changes))
        lines.append(f"  {i.title} ({names.get(i.source_id, '?')}): {what}")
    if len(updates) > 20:
        lines.append(f"  ...and {len(updates) - 20} more.")
    if not updates:
        lines.append("  None.")
    lines.append("")

    lines.append(f"LEFT OUT AUTOMATICALLY THIS WEEK: {len(built_in) + len(by_rule)}")
    if by_rule:
        lines.append("  By your skip rules:")
        lines += _rows(Counter(i.skip_reason.removeprefix("skip rule: ") for i in by_rule).most_common(), "    ")
    if built_in:
        lines.append("  By the built-in rules:")
        lines += _rows(Counter((i.skip_reason or "other").capitalize() for i in built_in).most_common(), "    ")
    if not (by_rule or built_in):
        lines.append("  None.")
    lines.append("")

    lines.append(f"COULDN'T BE READ, PLEASE CHECK BY HAND: {len(unreadable)}")
    for src in unreadable:
        at = src.last_checked_at and (src.last_checked_at if src.last_checked_at.tzinfo else src.last_checked_at.replace(tzinfo=UTC))
        tried = f" (last tried {_day(at.astimezone(CHICAGO))})" if at else ""
        page = src.pages[0] if src.pages else ""
        lines.append(f"  {src.name}: {src.last_error or src.last_status}{tried}" + (f"\n    {page}" if page else ""))
    if not unreadable:
        lines.append("  None. Every source could be read.")
    lines.append("")

    lines += [f"Checks this week: {checks}, {int(pages)} pages read, about ${cost_usd(int(tin), int(tout)):.2f} "
              "(estimated; the Anthropic Console has the exact bill).",
              f"This month so far: ${spent:.2f} of the ${budget:.2f} monthly budget."]
    if state == "saving":
        lines.append("  Over 80% of the budget: event pages wait until next month. Sources are still checked.")
    elif state == "paused":
        lines.append("  The budget is used up: only Check now runs until next month. Raise it on Event sources if needed.")
    lines += ["",
              "Event sources and how often each is checked:", f"{s.site_url.rstrip('/')}/#sources", "",
              "Culture Crew"]

    if quiet:
        subject = "Event discovery: nothing new this week"
    else:
        bits = [f"{len(found)} new"] + ([f"{len(updates)} {'update' if len(updates) == 1 else 'updates'}"] if updates else []) \
            + ([f"{len(unreadable)} to check by hand"] if unreadable else [])
        subject = "Event discovery: " + ", ".join(bits + (["budget used up"] if state == "paused" else []))
    return Email(to=s.recap_to, subject=subject, text="\n".join(lines) + "\n")
