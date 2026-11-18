"""CC-297: crew date polls. A member starts a poll for an event from one of their crews, picking showtimes inside a
window; crew-mates tick the ones that work; the starter locks one in. Only the crew's members ever see a poll, and
only first names (never emails).

- best_options: the top three options by how many current members ticked them (a tie goes to the earliest)
- ended: an open poll whose last option's day has passed reads as ended. Worked out on read, no job.
- poll_out: the response for one viewer
"""
import re
from datetime import date, timedelta

from sqlalchemy import delete, select

from .clock import today_in_chicago
from .models import Crew, DatePoll, DatePollAnswer, DatePollOption, DatePollPick, User

TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
MAX_OPTIONS = 40
MAX_WINDOW_DAYS = 60
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
    """The starter; or the crew's leader once the starter has left the crew."""
    if poll.started_by == user.id:
        return True
    starter_in = any(m.user_id == poll.started_by for m in crew.members)
    return not starter_in and crew.owner_id == user.id


def best_options(options: list[DatePollOption], picks: dict[int, list[str]], members_total: int, limit: int = 3) -> list[dict]:
    """picks: {option id: first names of current members who ticked it}. Options nobody ticked aren't "best"."""
    ranked = sorted((o for o in options if picks.get(o.id)), key=lambda o: (-len(picks[o.id]), o.date, o.time))
    return [{"option_id": o.id, "date": o.date, "time": o.time, "count": len(picks[o.id]), "names": sorted(picks[o.id]),
             "everyone": len(picks[o.id]) == members_total} for o in ranked[:limit]]


def poll_out(db, poll: DatePoll, crew: Crew, user: User) -> dict:
    members = {m.user_id: m.user for m in crew.members}
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
    return {"id": poll.id, "crew_id": crew.id, "crew_name": crew.name, "event_id": poll.event_id, "event_title": poll.event.title,
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
    db.execute(delete(DatePollAnswer).where(DatePollAnswer.user_id == user_id, DatePollAnswer.poll_id.in_(poll_ids)))


def recent_since(today: date) -> date:
    return today - timedelta(days=RECENT_DAYS)
