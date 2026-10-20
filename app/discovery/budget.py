"""The monthly budget for reading pages with Claude (CC-80). The product owner's target is about $10 a
month; at the expected size (about 200 sources) discovery costs about $5, so the guard is for the
unexpected: a very long page, or a site that changes all the time.

- under 80% of the budget: everything runs
- from 80%: "saving": event pages (the 60- and 45-day checks) wait until next month; sources still run
- at 100%: "paused": only sources an admin asked for with Check now are read

Spending is worked out from the tokens logged for each check this calendar month (Chicago time), at
Claude Haiku 5.5's list price (readers.cost_usd).
"""
from datetime import UTC, datetime

from sqlalchemy import func, select

from ..models import AppSetting, DiscoveryCheck
from ..stats import CHICAGO
from .readers import cost_usd

KEY = "discovery_budget_usd"
DEFAULT_USD = 10.0
SAVE_FROM = 0.8


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
