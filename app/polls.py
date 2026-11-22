"""CC-297: crew date polls. A member starts a poll for an event from one of their crews, picking showtimes inside a
window; crew-mates tick the ones that work; the crew leader locks one in. Only the crew's members ever see a poll, and
only first names (never emails).

- best_options: the top three options by how many current members ticked them (a tie goes to the earliest)
- ended: an open poll whose last option's day has passed reads as ended. Worked out on read, no job.
- poll_out: the response for one viewer

CC-298: an exhibition ("hours") poll has 1 to 5 day options; members tick half-hour blocks inside the day's hours, and
"best" is a range of neighbouring blocks (at least an hour) with the people free for all of it.
"""
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from .clock import today_in_chicago
from .models import Crew, CrewMember, DatePoll, DatePollAnswer, DatePollBlock, DatePollOption, DatePollPick, PlanShare, User

MAX_WINDOW_DAYS = 60
MAX_HOURS_DAYS = 5  # CC-298: days in an exhibition poll
HOURS_AHEAD_DAYS = 30  # ...chosen from today and the 29 days after
USUAL_HOURS = ("10:00", "17:00")  # when the venue's hours aren't known (as the site's USUAL)
MIN_RANGE = 2  # blocks: a range is at least an hour
_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RECENT_DAYS = 14  # how long a closed poll stays listed


def first_name(user: User | None) -> str:
    name = (user.display_name.strip() if user else "")
    return name.split()[0] if name else "A member"


def ended(poll: DatePoll, today: date | None = None) -> bool:
    today = today or today_in_chicago()
    return poll.status == "open" and all(o.date < today for o in poll.options)


def state(poll: DatePoll, today: date | None = None) -> str:
    return "ended" if ended(poll, today) else poll.status


def can_manage(poll: DatePoll, crew: Crew, user: User) -> bool:
    """CC-297: only the crew's leader locks a time in or cancels a poll, whoever started it."""
    return crew.owner_id == user.id


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def valid_hhmm(value) -> bool:
    return isinstance(value, str) and len(value) == 5 and value[2] == ":" and value[:2].isdigit() and value[3:].isdigit() and int(value[:2]) < 24 and int(value[3:]) < 60


def hours_for(hours: dict, day: date) -> tuple[str, str] | None:
    """CC-298: the venue's (opens, closes) on a day, or None when it's marked Closed. A day that's missing, or hours that don't
    make sense (close at or before open, not a time), count as not known: 10 AM to 5 PM, as the site does."""
    key = _DOW[day.weekday()]
    if key not in (hours or {}):
        return USUAL_HOURS
    got = hours[key]
    if got is None:
        return None
    if isinstance(got, (list, tuple)) and len(got) == 2 and valid_hhmm(got[0]) and valid_hhmm(got[1]) and _minutes(got[1]) > _minutes(got[0]):
        return got[0], got[1]
    return USUAL_HOURS


def slots(opens: str | None, closes: str | None) -> list[str]:
    """The half-hour blocks of a day, by start time: the first on or after opening, the last ending by closing."""
    if not opens or not closes:
        return []
    first = -(-_minutes(opens) // 30) * 30
    return [_hhmm(m) for m in range(first, _minutes(closes) - 29, 30)]


def valid_range(opens: str | None, closes: str | None, start: str, end: str) -> bool:
    """A locked range: on the half-hour grid, inside the day's hours, at least an hour."""
    grid = slots(opens, closes)
    if start not in grid or not valid_hhmm(end) or _minutes(end) - _minutes(start) < MIN_RANGE * 30:
        return False
    return _hhmm(_minutes(end) - 30) in grid


def best_ranges(options: list[DatePollOption], free: dict[int, dict[str, set]], names: dict, members_total: int, limit: int = 3,
                today: date | None = None) -> list[dict]:
    """free: {option id: {block start: ids of current members free then}}. A range is neighbouring blocks on one day, at least
    an hour; its people are those free for every block. Only ranges that can't be stretched by a block either side without losing
    one of those people are candidates. Ranked by most people, then longer, then earlier."""
    today = today or today_in_chicago()
    found = []
    for o in options:
        if o.date < today:
            continue
        grid = slots(o.opens, o.closes)
        sets = [free.get(o.id, {}).get(t, set()) for t in grid]
        for i in range(len(grid)):
            people = set(sets[i])
            for j in range(i + 1, len(grid)):
                people &= sets[j]
                if not people:
                    break
                if (i > 0 and people <= sets[i - 1]) or (j + 1 < len(grid) and people <= sets[j + 1]):
                    continue  # could be longer with the same people
                found.append((len(people), j - i + 1, o, grid[i], _hhmm(_minutes(grid[j]) + 30), set(people)))  # a copy: people keeps shrinking
    found.sort(key=lambda r: (-r[0], -r[1], r[2].date, r[3]))
    return [{"option_id": o.id, "date": o.date, "time": start, "end": end, "count": n, "names": sorted(names[u] for u in people),
             "everyone": n == members_total} for n, _, o, start, end, people in found[:limit]]


def best_options(options: list[DatePollOption], picks: dict[int, list[str]], members_total: int, limit: int = 3, today: date | None = None) -> list[dict]:
    """picks: {option id: first names of current members who ticked it}. Options nobody ticked, or that have passed, aren't "best"."""
    today = today or today_in_chicago()
    ranked = sorted((o for o in options if picks.get(o.id) and o.date >= today), key=lambda o: (-len(picks[o.id]), o.date, o.time))
    return [{"option_id": o.id, "date": o.date, "time": o.time, "count": len(picks[o.id]), "names": sorted(picks[o.id]),
             "everyone": len(picks[o.id]) == members_total} for o in ranked[:limit]]


def _hours_out(db, poll: DatePoll, members: dict, user: User) -> tuple[list[dict], list[dict], dict | None]:
    """CC-298: an exhibition poll's days with each half-hour's answers, the best ranges, and the locked range."""
    option_ids = [o.id for o in poll.options]
    free: dict[int, dict[str, set]] = {i: {} for i in option_ids}
    for option_id, user_id, start in db.execute(select(DatePollBlock.option_id, DatePollBlock.user_id, DatePollBlock.start).where(DatePollBlock.option_id.in_(option_ids))):
        if user_id in members:  # someone who's left doesn't count, whatever is still stored
            free[option_id].setdefault(start, set()).add(user_id)
    names = {uid: first_name(u) for uid, u in members.items()}
    options = []
    for o in poll.options:
        grid = slots(o.opens, o.closes)
        blocks = [{"start": t, "count": len(free[o.id].get(t, ())), "names": sorted(names[u] for u in free[o.id].get(t, ())),
                   "yours": user.id in free[o.id].get(t, ())} for t in grid]
        options.append({"id": o.id, "date": o.date, "time": "", "count": 0, "names": [], "yours": any(b["yours"] for b in blocks),
                        "opens": o.opens, "closes": o.closes, "blocks": blocks})
    best = best_ranges(poll.options, free, names, len(members))
    locked = None
    if poll.status == "locked" and poll.locked_start and poll.locked_end:
        o = next((x for x in poll.options if x.id == poll.locked_option_id), None)
        if o is not None:
            grid = slots(o.opens, o.closes)
            inside = [t for t in grid if poll.locked_start <= t and _minutes(t) + 30 <= _minutes(poll.locked_end)]
            who = set.intersection(*[free[o.id].get(t, set()) for t in inside]) if inside else set()
            locked = {"id": o.id, "date": o.date, "time": poll.locked_start, "end": poll.locked_end, "count": len(who),
                      "names": sorted(names[u] for u in who), "yours": user.id in who}
    return options, best, locked


def poll_out(db, poll: DatePoll, crew: Crew, user: User) -> dict:
    members = {m.user_id: m.user for m in crew.members}
    if poll.kind == "hours":
        options, best, locked = _hours_out(db, poll, members, user)
        answered = {uid for uid in db.scalars(select(DatePollAnswer.user_id).where(DatePollAnswer.poll_id == poll.id)) if uid in members}
        starter = members.get(poll.started_by)
        return {"id": poll.id, "crew_id": crew.id, "crew_name": crew.name, "event_id": poll.event_id, "event_title": poll.event.title, "kind": "hours",
                "status": state(poll), "window_from": poll.window_from, "window_to": poll.window_to,
                "started_by": first_name(starter) if starter else "A former member", "can_manage": can_manage(poll, crew, user),
                "members_total": len(members), "answered_count": len(answered), "you_answered": user.id in answered,
                "options": options, "best": best, "locked": locked, "created_at": poll.created_at, "closed_at": poll.closed_at}
    option_ids = [o.id for o in poll.options]
    by_option: dict[int, list[str]] = {i: [] for i in option_ids}
    mine: set[int] = set()
    for option_id, user_id in db.execute(select(DatePollPick.option_id, DatePollPick.user_id).where(DatePollPick.option_id.in_(option_ids))):
        if user_id in members:  # someone who's left doesn't count, whatever is still stored
            by_option[option_id].append(first_name(members[user_id]))
            if user_id == user.id:
                mine.add(option_id)
    answered = {uid for uid in db.scalars(select(DatePollAnswer.user_id).where(DatePollAnswer.poll_id == poll.id)) if uid in members}
    total = len(members)
    options = [{"id": o.id, "date": o.date, "time": o.time, "count": len(by_option[o.id]), "names": sorted(by_option[o.id]),
                "yours": o.id in mine} for o in poll.options]
    locked = next((o for o in options if o["id"] == poll.locked_option_id), None) if poll.status == "locked" else None
    starter = members.get(poll.started_by)
    return {"id": poll.id, "crew_id": crew.id, "crew_name": crew.name, "event_id": poll.event_id, "event_title": poll.event.title, "kind": "shows",
            "status": state(poll), "window_from": poll.window_from, "window_to": poll.window_to,
            "started_by": first_name(starter) if starter else "A former member", "can_manage": can_manage(poll, crew, user),
            "members_total": total, "answered_count": len(answered), "you_answered": user.id in answered,
            "options": options, "best": best_options(poll.options, by_option, total), "locked": locked,
            "created_at": poll.created_at, "closed_at": poll.closed_at}


def drop_member_answers(db, user_id, crew_id: int) -> None:
    """Someone left (or was removed from) the crew: their answers in its polls go with them. Doesn't commit."""
    poll_ids = select(DatePoll.id).where(DatePoll.crew_id == crew_id)
    option_ids = select(DatePollOption.id).where(DatePollOption.poll_id.in_(poll_ids))
    db.execute(delete(DatePollPick).where(DatePollPick.user_id == user_id, DatePollPick.option_id.in_(option_ids)))
    db.execute(delete(DatePollBlock).where(DatePollBlock.user_id == user_id, DatePollBlock.option_id.in_(option_ids)))
    db.execute(delete(DatePollAnswer).where(DatePollAnswer.user_id == user_id, DatePollAnswer.poll_id.in_(poll_ids)))


def recent_since(today: date) -> date:
    return today - timedelta(days=RECENT_DAYS)


def interest(db, event_id: int, crew_id: int) -> dict:
    """CC-297: how many of a crew's members have shared interest (or a going plan) in an event with it. A poll can be
    opened once more than half have: members choose which crews see their interest, so each crew counts its own."""
    shared = db.scalar(select(func.count()).select_from(PlanShare).where(PlanShare.event_id == event_id, PlanShare.crew_id == crew_id)) or 0
    total = db.scalar(select(func.count()).select_from(CrewMember).where(CrewMember.crew_id == crew_id)) or 0
    return {"crew_id": crew_id, "shared": shared, "members_total": total, "ready": total > 0 and shared * 2 > total}
