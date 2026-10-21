"""Plans shared with crews (CC-75). A member chooses, plan by plan, which of their crews can see it
(PlanShare); nothing is shared by default. Only members of a crew ever see what's shared with it.

- attach_crew_plans: on event cards, which of your crew-mates are interested or going ("2 from
  Thursday Theater are going"), per crew; never your own plan, never a crew you're not in
- crew_plans_condition: the catalog's filter for events your crews have plans for
- crew_board: a crew's shared plans by event, for its box on the account page
- CC-93: "I've been" marks, seen by the crews the plan was shared with when it was marked (AttendedShare),
  on the crew board under the plans line, with whether the member's photo from it is approved. A closed
  event stays on the board for BEEN_DAYS while someone's been.
"""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import delete, select

from .models import AttendedShare, Crew, CrewMember, Event, Plan, PlanShare, PlanStatus, User, WentPhoto
from .visibility import live_condition


def name_of(user: User | None) -> str:
    return (user.display_name.strip() if user else "") or "A member without a name"


def my_crew_ids(db, user: User) -> set[int]:
    return set(db.scalars(select(CrewMember.crew_id).where(CrewMember.user_id == user.id)))


def shared_with(db, user: User, event_ids: set[int]) -> dict[int, list[int]]:
    """{event id: [crew ids]} for your own plans."""
    out = defaultdict(list)
    for event_id, crew_id in db.execute(select(PlanShare.event_id, PlanShare.crew_id)
                                        .where(PlanShare.user_id == user.id, PlanShare.event_id.in_(event_ids))):
        out[event_id].append(crew_id)
    return {k: sorted(v) for k, v in out.items()}


def share_plan(db, user: User, event_id: int, crew_ids: list[int]) -> None:
    """Share your plan for this event with exactly these crews (all of them yours). Doesn't commit."""
    db.execute(delete(PlanShare).where(PlanShare.user_id == user.id, PlanShare.event_id == event_id))
    for crew_id in sorted(set(crew_ids)):
        db.add(PlanShare(user_id=user.id, event_id=event_id, crew_id=crew_id))


def unshare(db, user_id, event_id: int | None = None, crew_id: int | None = None) -> None:
    """Drop shares: for a plan that's gone, or everything shared with a crew someone left. Doesn't commit."""
    q = delete(PlanShare).where(PlanShare.user_id == user_id)
    if event_id is not None:
        q = q.where(PlanShare.event_id == event_id)
    if crew_id is not None:
        q = q.where(PlanShare.crew_id == crew_id)
    db.execute(q)


BEEN_DAYS = 14


def share_been(db, user_id, event_id: int) -> None:
    """CC-93: the crews this plan is shared with see the "I've been" mark. Call before the plan goes. Doesn't commit."""
    have = set(db.scalars(select(AttendedShare.crew_id).where(AttendedShare.user_id == user_id, AttendedShare.event_id == event_id)))
    for crew_id in db.scalars(select(PlanShare.crew_id).where(PlanShare.user_id == user_id, PlanShare.event_id == event_id)):
        if crew_id not in have:
            db.add(AttendedShare(user_id=user_id, event_id=event_id, crew_id=crew_id))


def unshare_been(db, user_id, crew_id: int) -> None:
    """CC-93: someone left the crew; their marks leave with them. Doesn't commit."""
    db.execute(delete(AttendedShare).where(AttendedShare.user_id == user_id, AttendedShare.crew_id == crew_id))


def _been_rows(db, crew_ids: set[int]):
    """(event id, crew id, user, photo status or None) for marks seen by these crews, from current members."""
    return db.execute(select(AttendedShare.event_id, AttendedShare.crew_id, User, WentPhoto.status)
                      .join(User, User.id == AttendedShare.user_id)
                      .join(CrewMember, (CrewMember.crew_id == AttendedShare.crew_id) & (CrewMember.user_id == AttendedShare.user_id))
                      .outerjoin(WentPhoto, (WentPhoto.user_id == AttendedShare.user_id) & (WentPhoto.event_id == AttendedShare.event_id))
                      .where(AttendedShare.crew_id.in_(crew_ids))).all()


def _shared_rows(db, crew_ids: set[int], event_ids: set[int] | None = None):
    """(event id, crew id, crew name, user, status, going on) for plans shared with these crews."""
    q = (select(PlanShare.event_id, Crew.id, Crew.name, User, Plan.status, Plan.going_on)
         .join(Crew, Crew.id == PlanShare.crew_id)
         .join(Plan, (Plan.user_id == PlanShare.user_id) & (Plan.event_id == PlanShare.event_id))
         .join(User, User.id == PlanShare.user_id)
         .join(CrewMember, (CrewMember.crew_id == PlanShare.crew_id) & (CrewMember.user_id == PlanShare.user_id))
         .where(PlanShare.crew_id.in_(crew_ids)))
    if event_ids is not None:
        q = q.where(PlanShare.event_id.in_(event_ids))
    return db.execute(q).all()


def attach_crew_plans(db, events, user: User | None):
    """Set event.crew_plans on each event: per crew of yours, who else is going or interested. Returns the events."""
    events = [e for e in events if e is not None]
    for e in events:
        e.crew_plans = []
    if user is None or not events:
        return events
    crews = my_crew_ids(db, user)
    if not crews:
        return events
    by_event: dict[int, dict[int, dict]] = defaultdict(dict)
    for event_id, crew_id, crew_name, who, status, going_on in _shared_rows(db, crews, {e.id for e in events}):
        if who.id == user.id:
            continue
        entry = by_event[event_id].setdefault(crew_id, {"crew_id": crew_id, "crew_name": crew_name, "going": [], "interested": []})
        entry["going" if status == PlanStatus.going else "interested"].append(name_of(who))
    for e in events:
        e.crew_plans = sorted(by_event.get(e.id, {}).values(), key=lambda c: c["crew_name"].lower())
    return events


def crew_plans_condition(db, user: User, crew_id: int | None = None):
    """SQL condition on Event.id: events your crews (or one of them) have shared plans or "I've been" marks
    for, from other members. CC-94: a crew-mate who's been counts too (Your Crews Catalog)."""
    crews = my_crew_ids(db, user)
    if crew_id is not None:
        crews &= {crew_id}
    members_plans = (select(PlanShare.event_id)
                     .join(CrewMember, (CrewMember.crew_id == PlanShare.crew_id) & (CrewMember.user_id == PlanShare.user_id))
                     .where(PlanShare.crew_id.in_(crews or {-1}), PlanShare.user_id != user.id))
    members_been = (select(AttendedShare.event_id)
                    .join(CrewMember, (CrewMember.crew_id == AttendedShare.crew_id) & (CrewMember.user_id == AttendedShare.user_id))
                    .where(AttendedShare.crew_id.in_(crews or {-1}), AttendedShare.user_id != user.id))
    return Event.id.in_(members_plans) | Event.id.in_(members_been)


def crew_board(db, crew_ids: set[int], viewer: User, today: date) -> dict[int, list[dict]]:
    """{crew id: [{"event": Event, "people": [...], "been": [...]}]}: each crew's shared plans for live, upcoming
    events, one entry per event, soonest first. A "going" plan for a day that's passed drops off. been (CC-93):
    crew-mates who've been, never the viewer, with an approved photo first; a closed event stays BEEN_DAYS."""
    if not crew_ids:
        return {}
    since = today - timedelta(days=BEEN_DAYS)
    ends = dict(db.execute(select(Event.id, Event.end_date).where(live_condition(), (Event.end_date.is_(None)) | (Event.end_date >= since))).all())
    live = {i for i, end in ends.items() if end is None or end >= today}
    boards: dict[int, dict[int, dict]] = defaultdict(dict)
    for event_id, crew_id, _name, who, status, going_on in _shared_rows(db, crew_ids):
        if event_id not in live or (status == PlanStatus.going and going_on and going_on < today):
            continue
        entry = boards[crew_id].setdefault(event_id, {"event_id": event_id, "people": [], "been": []})
        entry["people"].append({"name": name_of(who), "status": status, "date": going_on, "you": who.id == viewer.id})
    for event_id, crew_id, who, photo in _been_rows(db, crew_ids):
        if event_id not in ends or who.id == viewer.id:
            continue
        entry = boards[crew_id].setdefault(event_id, {"event_id": event_id, "people": [], "been": []})
        entry["been"].append({"name": name_of(who), "approved": photo == "approved"})
    events = {e.id: e for e in db.scalars(select(Event).where(Event.id.in_({k for b in boards.values() for k in b})))}

    def when(entry):
        going = [p["date"] for p in entry["people"] if p["date"]]
        ev = events[entry["event_id"]]
        closed = ev.end_date is not None and ev.end_date < today  # kept only because someone's been: last
        return (closed, min(going) if going else ev.end_date or ev.begin_date or date.max, ev.title.lower())

    out = {}
    for crew_id, entries in boards.items():
        rows = []
        for entry in sorted(entries.values(), key=when):
            people = sorted(entry["people"], key=lambda p: (p["status"] != PlanStatus.going, p["date"] or date.max, p["name"].lower()))
            been = sorted(entry["been"], key=lambda b: (not b["approved"], b["name"].lower()))
            rows.append({"event": events[entry["event_id"]], "people": people, "been": been})
        out[crew_id] = rows
    return out
