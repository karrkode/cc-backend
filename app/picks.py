"""The owner's picks (CC-66): plans made by accounts with the dolphins setting (CC-61) are shown on
event cards to every visitor, as the dolphins. Everyone else's plans stay private (CC-54).

An event is a "going" pick if any such account is going (the soonest date counts), otherwise an
"interested" pick if any such account is interested.
"""
from datetime import date

from sqlalchemy import or_, select

from .models import Event, Plan, PlanStatus, User


def _owner_plans():
    return select(Plan.event_id).join(User, User.id == Plan.user_id).where(User.dolphins)


def pick_condition(kinds: list[PlanStatus]):
    """SQL condition on Event.id: events that are picks of these kinds."""
    going = _owner_plans().where(Plan.status == PlanStatus.going)
    interested = _owner_plans().where(Plan.status == PlanStatus.interested, Plan.event_id.not_in(going))
    parts = []
    if PlanStatus.going in kinds:
        parts.append(Event.id.in_(going))
    if PlanStatus.interested in kinds:
        parts.append(Event.id.in_(interested))
    return or_(*parts)


def attach_picks(db, events):
    """Set event.pick on each event (a dict for EventOut.pick, or None). Returns the events."""
    events = [e for e in events if e is not None]
    if not events:
        return events
    rows = db.execute(select(Plan.event_id, Plan.status, Plan.going_on, User.display_name)
                      .join(User, User.id == Plan.user_id)
                      .where(User.dolphins, Plan.event_id.in_({e.id for e in events}))).all()
    best: dict[int, dict] = {}
    for event_id, status, going_on, name in rows:
        cur = best.get(event_id)
        rank = (0 if status == PlanStatus.going else 1, going_on or date.max)
        if cur is None or rank < cur["_rank"]:
            best[event_id] = {"status": status, "date": going_on, "by": (name or "").strip(), "_rank": rank}
    for e in events:
        p = best.get(e.id)
        e.pick = None if p is None else {k: v for k, v in p.items() if k != "_rank"}
    return events

