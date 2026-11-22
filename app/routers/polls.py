"""CC-297, CC-298: crew date polls. Every route is for the crew's members only; to anyone else a crew or poll doesn't exist.
The browser works out the options from the event's schedule (as "Commit to" does). The server checks that they are
possible (inside the window and the event's dates, today or later, a valid time), not that each is a real showtime."""
from datetime import UTC, datetime, time

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from ..clock import today_in_chicago, utcnow
from ..deps import DB, CurrentUser
from ..models import Crew, CrewMember, DatePoll, DatePollAnswer, DatePollBlock, DatePollOption, DatePollPick, Event, PlanShare
from ..polls import (HOURS_AHEAD_DAYS, MAX_WINDOW_DAYS, can_manage, ended, hours_for, interest, open_days, poll_out, recent_since, slots, valid_range)
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


def is_exhibition(event: Event) -> bool:
    """CC-298: Visual Art with no listed showtimes gets an hours poll (the site's isExhibit); anything else gets a showtime poll."""
    return event.category == "Visual Art" and not event.schedule["dates"]


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


@router.get("/poll-ready")
def poll_ready(db: DB, user: CurrentUser):
    """CC-297: the events a majority of one of your crews has shared interest in that have no poll yet. A poll opens by
    itself: the site starts it (it knows the event's showtimes) the first time a member's page sees one of these."""
    today = today_in_chicago()
    out = []
    crews = {c.id: c.name for c in db.scalars(select(Crew).join(CrewMember).where(CrewMember.user_id == user.id))}
    for crew_id in db.scalars(select(CrewMember.crew_id).where(CrewMember.user_id == user.id)).all():
        total = db.scalar(select(func.count()).select_from(CrewMember).where(CrewMember.crew_id == crew_id)) or 0
        for event_id, n in db.execute(select(PlanShare.event_id, func.count()).where(PlanShare.crew_id == crew_id).group_by(PlanShare.event_id)).all():
            if n * 2 <= total:
                continue
            event = db.get(Event, event_id)
            if event is None or not is_live(event) or (event.end_date and event.end_date < today):
                continue
            if is_exhibition(event) and not open_days(event, today):
                continue  # nothing to pick a day from yet (it opens later, or the venue is closed), so nothing to wait on
            if db.scalar(select(DatePoll.id).where(DatePoll.crew_id == crew_id, DatePoll.event_id == event_id).limit(1)) is None:
                out.append({"crew_id": crew_id, "event_id": event_id, "kind": "hours" if is_exhibition(event) else "shows",
                            "event_title": event.title, "crew_name": crews[crew_id]})
    return out


def _hours_options(event: Event, body: PollIn, today) -> tuple:
    """CC-298: 1 to 5 distinct days, from today to the 29 after, while the event runs and the venue is open; each keeps that day's hours."""
    days = body.days
    if body.options or not days or len(set(days)) != len(days):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose between one and five different days.")
    venue_hours = event.schedule["hours"]
    running = set(open_days(event, today)) | {x for x in days if hours_for(venue_hours, x) is None}  # a closed day gets its own message below
    options = []
    for day in sorted(days):
        if day not in running:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Choose days in the next {HOURS_AHEAD_DAYS} days while the exhibition runs.")
        got = hours_for(venue_hours, day)
        if got is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The venue is closed on one of those days.")
        options.append(DatePollOption(date=day, time="", opens=got[0], closes=got[1]))
    return options[0].date, options[-1].date, options


@router.post("/{crew_id}/polls", response_model=PollOut, status_code=status.HTTP_201_CREATED)
def start_poll(crew_id: int, body: PollIn, db: DB, user: CurrentUser):
    crew = _member_crew(db, crew_id, user)
    event = db.get(Event, body.event_id)
    if event is None or not is_live(event):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    hours = is_exhibition(event)
    if hours != (body.kind == "hours"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Pick days for an exhibition, and showtimes for a performance." if hours else "Pick showtimes for a performance.")
    if not interest(db, event.id, crew.id)["ready"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "A poll opens once more than half of the crew has shared interest in this event.")
    today = today_in_chicago()
    if hours:
        lo, hi, options = _hours_options(event, body, today)
    else:
        lo, hi = body.window_from, body.window_to
        if lo is None or hi is None or not body.options or body.days:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Send a window and at least one showtime.")
        if hi < lo or (hi - lo).days >= MAX_WINDOW_DAYS or hi < today:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Choose a window of at most {MAX_WINDOW_DAYS} days that hasn't passed.")
        seen = set()
        for o in body.options:
            if not lo <= o.date <= hi or o.date < today or (event.begin_date and o.date < event.begin_date) or (event.end_date and o.date > event.end_date):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Every option must be an upcoming date in the window, while the event runs.")
            if (o.date, o.time) in seen:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The same showtime is listed twice.")
            seen.add((o.date, o.time))
        options = [DatePollOption(date=o.date, time=o.time) for o in sorted(body.options, key=lambda o: (o.date, o.time))]
    # CC-297: a crew gets one poll for an event. A locked or cancelled one isn't reopened by the next page that sees the majority.
    for old in db.scalars(select(DatePoll).where(DatePoll.crew_id == crew.id, DatePoll.event_id == event.id, DatePoll.status.in_(("open", "locked", "cancelled")))):
        if old.status != "open" or not ended(old, today):
            raise HTTPException(status.HTTP_409_CONFLICT, "This crew already has a poll for this event.")
        old.status = "ended"  # one that's run out of dates mustn't block the next; it stays "ended", not "cancelled"
        old.closed_at = datetime.combine(max(o.date for o in old.options), time.min, tzinfo=UTC)
    if not poll_limiter.hit(str(user.id), POLLS_PER_DAY):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of polls for one day. Try again tomorrow.")
    poll = DatePoll(crew_id=crew.id, event_id=event.id, started_by=user.id, window_from=lo, window_to=hi, kind=body.kind, options=options)
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
    if poll.kind == "hours":
        if body.option_ids:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Mark half-hours for this poll, not showtimes.")
        grids = {o.id: set(slots(o.opens, o.closes)) for o in poll.options}
        chosen = {(b.option_id, b.start) for b in body.blocks}
        if any(oid not in grids or start not in grids[oid] for oid, start in chosen):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose half-hours inside the venue's hours on this poll's days.")
        for block in db.scalars(select(DatePollBlock).where(DatePollBlock.user_id == user.id, DatePollBlock.option_id.in_(mine))):
            db.delete(block)
        db.flush()
        for oid, start in chosen:
            db.add(DatePollBlock(option_id=oid, user_id=user.id, start=start))
    else:
        if body.blocks:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose showtimes for this poll, not half-hours.")
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
    if poll.kind == "hours":
        if not body.start or not body.end or not valid_range(chosen.opens, chosen.closes, body.start, body.end):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a range of at least an hour, on the half-hour, inside the venue's hours.")
        poll.locked_start, poll.locked_end = body.start, body.end
    elif body.start or body.end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A showtime poll locks one of its showtimes.")
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
