"""CC-301: a theater event with a big Show Run, for trying the Show Run editor by hand. Local only.

    python -m scripts.seed_show_run_qa

Makes (or resets) the approved Theater event "Show Run QA" with 37 shows over eight weeks, Thu Nov 5 to Dec 26 2026,
all origin admin (an exact Show Run). Some days are already past, so past-day handling can be tried too.

  Every week           Thu 8 PM, Fri 8 PM, Sat 3 PM and Sat 8 PM (two shows on a Saturday)
  Weeks 1, 3, 5, 7     Sun 3 PM (Nov 8, Nov 22, Dec 6, Dec 20)
  Weeks 2, 4, 6, 8     Wed 7:30 PM (Nov 11, Nov 25, Dec 9, Dec 23)
  Dark                 Thu Nov 26 (Thanksgiving), Thu Dec 24, Fri Dec 25

Running it again puts the shows back as they were. Only works with ENV=local: it never runs in production.
"""
from datetime import date, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Area, Event, EventRunDate, EventStatus, Venue

TITLE = "Show Run QA"
VENUE = ("Show Run QA Theater", "Lincoln Park")
FIRST_THURSDAY = date(2026, 11, 5)
DARK = {date(2026, 11, 26), date(2026, 12, 24), date(2026, 12, 25)}


def qa_shows() -> list[tuple[date, str]]:
    """The 37 (day, time) shows, sorted."""
    shows = []
    for week in range(8):
        thu = FIRST_THURSDAY + timedelta(weeks=week)
        mon = thu - timedelta(days=3)  # weeks run Monday to Sunday
        days = [(thu, "20:00"), (thu + timedelta(days=1), "20:00"), (thu + timedelta(days=2), "15:00"),
                (thu + timedelta(days=2), "20:00")]
        days.append((mon + timedelta(days=6), "15:00") if week % 2 == 0 else (mon + timedelta(days=2), "19:30"))
        shows += [(d, t) for d, t in days if d not in DARK]
    return sorted(shows)


def seed(db) -> Event:
    """Make the event, or put its shows back. Returns it."""
    venue = db.scalar(select(Venue).where(Venue.name == VENUE[0], Venue.neighborhood == VENUE[1]))
    if venue is None:
        venue = Venue(name=VENUE[0], neighborhood=VENUE[1], area=Area.city, category="Theater")
        db.add(venue)
        db.flush()
    event = db.scalar(select(Event).where(Event.title == TITLE))
    if event is None:
        event = Event(title=TITLE, venue_id=venue.id, category="Theater", status=EventStatus.approved,
                      url="https://example.org/show-run-qa", cost_text="Free")
        db.add(event)
    shows = qa_shows()
    event.shows.clear()
    db.flush()  # the old shows go first, so a re-run doesn't clash with itself
    event.shows = [EventRunDate(date=d, time=t, origin="admin") for d, t in shows]
    event.begin_date, event.end_date, event.open_run, event.weekly, event.weeks = shows[0][0], shows[-1][0], False, {}, {}
    db.commit()
    return event


def main():
    env = get_settings().env
    if env != "local":
        raise SystemExit(f"seed_show_run_qa only runs with ENV=local (this is ENV={env}).")
    with SessionLocal() as db:
        event = seed(db)
        print(f'"{TITLE}" is event {event.id}: {len(event.shows)} shows, {event.begin_date} to {event.end_date}.')


if __name__ == "__main__":
    main()
