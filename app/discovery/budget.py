"""The monthly budget for reading pages with Claude (CC-80). The product owner's target is about $10 a
month; at the expected size (about 200 sources) discovery costs about $5, so the guard is for the
unexpected: a very long page, or a site that changes all the time.

- under 80% of the budget: everything runs
- from 80%: "saving": event pages (the 60- and 45-day checks) wait until next month; sources still run,
  the ones with the most approved events per dollar first (CC-211), so what's left goes to the best ones
- at 100%: "paused": only sources an admin asked for with Check now are read

CC-211: the first time a month reaches 80%, admins get one email (settings.recap_to, like the recap).

Spending is worked out from the tokens logged for each check this calendar month (Chicago time), at
Claude Haiku 5.5's list price (readers.cost_usd). The alert is only as right as that price.
"""
from datetime import UTC, datetime

from sqlalchemy import func, select

from ..config import get_settings
from ..mail import Email, send
from ..models import AppSetting, DiscoveredItem, DiscoveryCheck, Event, EventStatus
from ..stats import CHICAGO
from .readers import cost_usd

KEY = "discovery_budget_usd"
DEFAULT_USD = 10.0
SAVE_FROM = 0.8
ALERT_KEY = "discovery_budget_alert"
# CC-211: every source starts as if it had this many approved events at the all-sources rate. A new source
# (or a free one with nothing approved yet) ranks at the average, and a few checks can't swing it far.
STARTING_EVENTS = 3


def monthly_budget(db) -> float:
    row = db.get(AppSetting, KEY)
    try:
        return float(row.value["usd"]) if row else DEFAULT_USD
    except (KeyError, TypeError, ValueError):
        return DEFAULT_USD


def set_monthly_budget(db, usd: float) -> None:
    row = db.get(AppSetting, KEY)
    if row is None:
        db.add(AppSetting(key=KEY, value={"usd": usd}))
    else:
        row.value = {"usd": usd}


def month_start(now: datetime) -> datetime:
    local = now.astimezone(CHICAGO)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def spent_this_month(db, now: datetime | None = None) -> float:
    since = month_start(now or datetime.now(UTC))
    tin, tout = db.execute(select(func.coalesce(func.sum(DiscoveryCheck.input_tokens), 0),
                                  func.coalesce(func.sum(DiscoveryCheck.output_tokens), 0))
                           .where(DiscoveryCheck.checked_at >= since)).one()
    return cost_usd(int(tin), int(tout))


def budget_state(db, now: datetime | None = None) -> tuple[str, float, float]:
    """("ok" | "saving" | "paused", spent this month, the monthly budget)."""
    spent, budget = spent_this_month(db, now), monthly_budget(db)
    state = "paused" if spent >= budget else "saving" if spent >= budget * SAVE_FROM else "ok"
    return state, spent, budget


def best_first(db, sources: list) -> list:
    """CC-211: Check now first (it runs whatever the budget says), then the most approved events per dollar.
    Only events still live count, and a source with structured data or its own API costs little, so it ranks high."""
    approved = dict(db.execute(select(DiscoveredItem.source_id, func.count())
                               .join(Event, DiscoveredItem.event_id == Event.id)
                               .where(DiscoveredItem.status == "approved", Event.status == EventStatus.approved,
                                      DiscoveredItem.source_id.is_not(None))
                               .group_by(DiscoveredItem.source_id)).all())
    costs = {sid: cost_usd(int(tin), int(tout)) for sid, tin, tout in db.execute(
        select(DiscoveryCheck.source_id, func.sum(DiscoveryCheck.input_tokens), func.sum(DiscoveryCheck.output_tokens))
        .where(DiscoveryCheck.source_id.is_not(None)).group_by(DiscoveryCheck.source_id))}
    all_approved, all_cost = sum(approved.values()), sum(costs.values())

    def per_dollar(source) -> float:
        if not all_approved or not all_cost:
            return 0  # nothing to compare yet
        return (approved.get(source.id, 0) + STARTING_EVENTS) / (costs.get(source.id, 0) + STARTING_EVENTS * all_cost / all_approved)

    return sorted(sources, key=lambda s: (not s.check_requested, -per_dollar(s), s.id))


def alert_email(state: str, spent: float, budget: float) -> Email:
    s = get_settings()
    now_what = ("Only Check now runs until next month." if state == "paused" else
                "Event pages wait until next month. Sources are still checked, with the most approved events "
                "per dollar first. At 100% of the budget, only Check now runs.")
    return Email(to=s.recap_to,
                 subject="Event discovery: budget used up" if state == "paused" else "Event discovery: over 80% of the budget spent",
                 text=f"${spent:.2f} of the ${budget:.0f} monthly budget is spent.\n\n{now_what}\n\n"
                      f"To change the budget, go to Event sources: {s.site_url.rstrip('/')}/#sources\n\nCulture Crew\n")


def alert_if_short(db, now: datetime | None = None) -> bool:
    """CC-211: email admins the first time this month's spend reaches 80% (once a month, and once more if
    the budget is changed and then reached too). A failed send is tried again on the next run."""
    now = now or datetime.now(UTC)
    state, spent, budget = budget_state(db, now)
    sent = {"month": f"{now.astimezone(CHICAGO):%Y-%m}", "usd": budget}
    row = db.get(AppSetting, ALERT_KEY)
    if state == "ok" or (row and row.value == sent):
        return False
    try:
        send(alert_email(state, spent, budget), strict=True)
    except Exception:  # noqa: BLE001 - email trouble must not stop discovery; mail.send has logged it
        return False
    if row is None:
        db.add(AppSetting(key=ALERT_KEY, value=sent))
    else:
        row.value = sent
    db.commit()
    return True
