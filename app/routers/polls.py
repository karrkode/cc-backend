"""CC-297: crew date polls. Every route is for the crew's members only; to anyone else a crew or poll doesn't exist.
The browser works out the options from the event's schedule (as "Commit to" does). The server checks that they are
possible (inside the window and the event's dates, today or later, a valid time), not that each is a real showtime."""
from datetime import UTC, datetime, time

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..clock import today_in_chicago, utcnow
from ..deps import DB, CurrentUser
from ..models import Crew, CrewMember, DatePoll, DatePollAnswer, DatePollOption, DatePollPick, Event
from ..polls import MAX_WINDOW_DAYS, can_manage, ended, interest, poll_out, recent_since
from ..ratelimit import SlidingWindowLimiter
from ..schemas import PollAnswerIn, PollIn, PollLockIn, PollOut
from ..visibility import is_live
from .crews import _member_crew

router = APIRouter(prefix="/crews", tags=["crews"])

POLLS_PER_DAY = 20
poll_limiter = SlidingWindowLimiter(window_seconds=86400)
NOT_FOUND = "Poll not found"


def _poll(db: DB, crew: Crew, poll_id: int, write: bool = False) -> DatePoll:
    # write: the row is locked until the commit, so an answer, a lock and a cancel can't slip past each other (no-op on SQLite)
    if write and db.scalar(select(DatePoll.id).where(DatePoll.id == poll_id).with_for_update()) is None:  # Postgres can't lock the joined event row
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    poll = db.get(DatePoll, poll_id)
    if poll is None or poll.crew_id != crew.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return poll


def _open(poll: DatePoll) -> None:
    if poll.status != "open":
        raise HTTPException(status.HTTP_409_CONFLICT, "This poll is closed.")
    if ended(poll):
        raise HTTPException(status.HTTP_409_CONFLICT, "Every date in this poll has passed.")


@router.get("/polls", response_model=list[PollOut])
def my_polls(db: DB, user: CurrentUser, event_id: int | None = None, waiting: bool = False):
    """Open polls (and ones closed in the last two weeks) across your crews. waiting=true: only those you haven't answered."""
    crews = {c.id: c for c in db.scalars(select(Crew).join(CrewMember).where(CrewMember.user_id == user.id))}
    if not crews:
        return []
    today = today_in_chicago()
    since_at = datetime.combine(recent_since(today), time.min, tzinfo=UTC)
    q = select(DatePoll).where(DatePoll.crew_id.in_(crews), or_(DatePoll.status == "open", DatePoll.closed_at >= since_at)).order_by(DatePoll.created_at.desc(), DatePoll.id.desc())
    if event_id is not None:
        q = q.where(DatePoll.event_id == event_id)
    out = []
    since = recent_since(today)
    for poll in db.scalars(q):
        closed_on = max(o.date for o in poll.options)
        if (poll.status == "open" and closed_on < since) or (poll.status != "open" and (poll.closed_at is None or poll.closed_at.date() < since)):
            continue
        row = poll_out(db, poll, crews[poll.crew_id], user)
        if waiting and (row["status"] != "open" or row["you_answered"]):
            continue
        out.append(row)
    return out


@router.get("/{crew_id}/polls", response_model=list[PollOut])
def crew_polls(crew_id: int, db: DB, user: CurrentUser):
    crew = _member_crew(db, crew_id, user)
    since = recent_since(today_in_chicago())
    at = datetime.combine(since, time.min, tzinfo=UTC)
    rows = [poll_out(db, p, crew, user) for p in db.scalars(select(DatePoll).where(DatePoll.crew_id == crew.id, or_(DatePoll.status == "open", DatePoll.closed_at >= at))
                                                           .order_by(DatePoll.created_at.desc(), DatePoll.id.desc()))
            if (max(o.date for o in p.options) >= since if p.status == "open" else p.closed_at is not None and p.closed_at.date() >= since)]
    return rows


@router.get("/poll-interest")
def poll_interest(event_id: int, db: DB, user: CurrentUser):
    """For each of your crews: how many members have shared interest in this event, and whether that's enough for a poll."""
    ids = db.scalars(select(CrewMember.crew_id).where(CrewMember.user_id == user.id)).all()
    return [interest(db, event_id, cid) for cid in ids]


@router.post("/{crew_id}/polls", response_model=PollOut, status_code=status.HTTP_201_CREATED)
def start_poll(crew_id: int, body: PollIn, db: DB, user: CurrentUser):
    crew = _member_crew(db, crew_id, user)
    event = db.get(Event, body.event_id)
    if event is None or not is_live(event):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    if event.category == "Visual Art" and not event.schedule["dates"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Date polls are for performances, not exhibitions.")
    if not interest(db, event.id, crew.id)["ready"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "A poll opens once more than half of the crew has shared interest in this event.")
    today = today_in_chicago()
    lo, hi = body.window_from, body.window_to
    if hi < lo or (hi - lo).days >= MAX_WINDOW_DAYS or hi < today:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Choose a window of at most {MAX_WINDOW_DAYS} days that hasn't passed.")
    seen = set()
    for o in body.options:
        if not lo <= o.date <= hi or o.date < today or (event.begin_date and o.date < event.begin_date) or (event.end_date and o.date > event.end_date):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Every option must be an upcoming date in the window, while the event runs.")
        if (o.date, o.time) in seen:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The same showtime is listed twice.")
        seen.add((o.date, o.time))
    for old in db.scalars(select(DatePoll).where(DatePoll.crew_id == crew.id, DatePoll.event_id == event.id, DatePoll.status == "open")):
        if not ended(old, today):
            raise HTTPException(status.HTTP_409_CONFLICT, "This crew already has an open poll for this event.")
        old.status = "ended"  # one that's run out of dates mustn't block the next; it stays "ended", not "cancelled"
        old.closed_at = datetime.combine(max(o.date for o in old.options), time.min, tzinfo=UTC)
    if not poll_limiter.hit(str(user.id), POLLS_PER_DAY):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of polls for one day. Try again tomorrow.")
    poll = DatePoll(crew_id=crew.id, event_id=event.id, started_by=user.id, window_from=lo, window_to=hi,
                    options=[DatePollOption(date=o.date, time=o.time) for o in sorted(body.options, key=lambda o: (o.date, o.time))])
    db.add(poll)
    try:
        db.commit()  # the partial unique index settles two starts at once
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "This crew already has an open poll for this event.")
    db.refresh(poll)
    return poll_out(db, poll, crew, user)


@router.put("/{crew_id}/polls/{poll_id}/answer", response_model=PollOut)
def answer(crew_id: int, poll_id: int, body: PollAnswerIn, db: DB, user: CurrentUser):
    """Replace your ticks. An empty list is an answer too: none of these work."""
    crew = _member_crew(db, crew_id, user)
    poll = _poll(db, crew, poll_id, write=True)
    _open(poll)
    mine = {o.id for o in poll.options}
    ids = set(body.option_ids)
    if not ids <= mine:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose from this poll's options.")
    for pick in db.scalars(select(DatePollPick).where(DatePollPick.user_id == user.id, DatePollPick.option_id.in_(mine))):
        db.delete(pick)
    db.flush()
    for option_id in ids:
        db.add(DatePollPick(option_id=option_id, user_id=user.id))
    existing = db.get(DatePollAnswer, (poll.id, user.id))
    if existing is None:
        db.add(DatePollAnswer(poll_id=poll.id, user_id=user.id))
    else:
        existing.answered_at = utcnow()
    try:
        db.commit()
    except IntegrityError:  # two answers from you at once: the later one is asked to retry
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Your answer was saving already. Try again.")
    db.refresh(poll)
    return poll_out(db, poll, crew, user)


def _manage(db: DB, crew_id: int, poll_id: int, user) -> tuple[Crew, DatePoll]:
    crew = _member_crew(db, crew_id, user)
    poll = _poll(db, crew, poll_id, write=True)
    if not can_manage(poll, crew, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the crew leader can do that")
    _open(poll)
    return crew, poll


@router.post("/{crew_id}/polls/{poll_id}/lock", response_model=PollOut)
def lock(crew_id: int, poll_id: int, body: PollLockIn, db: DB, user: CurrentUser):
    crew, poll = _manage(db, crew_id, poll_id, user)
    chosen = next((o for o in poll.options if o.id == body.option_id), None)
    if chosen is None or chosen.date < today_in_chicago():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose one of this poll's upcoming times.")
    poll.status, poll.locked_option_id, poll.closed_at = "locked", body.option_id, utcnow()
    db.commit()
    db.refresh(poll)
    return poll_out(db, poll, crew, user)


@router.post("/{crew_id}/polls/{poll_id}/cancel", response_model=PollOut)
def cancel(crew_id: int, poll_id: int, db: DB, user: CurrentUser):
    crew, poll = _manage(db, crew_id, poll_id, user)
    poll.status, poll.closed_at = "cancelled", utcnow()
    db.commit()
    db.refresh(poll)
    return poll_out(db, poll, crew, user)
