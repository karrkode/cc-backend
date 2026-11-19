"""CC-301: the Show Run: event_run_dates becomes the one store of an event's shows.

Revision ID: 0060
Revises: 0059
Create Date: 2026-11-19

Upgrade: adds `origin` (admin | member | found), swaps the unique constraint from (event_id, date) to
(event_id, date, time) so a matinee and an evening can share a day, and copies each event's dates from the
frozen `events.performances` JSON column into the table as `found` rows. The `events` rows are not touched.

Downgrade: writes the found rows back to `events.performances`, keeps one timed show a day, restores the old
constraint. Lossy only for what was saved after the upgrade: a second show on a day, or a show with no time.

No app code is imported, so this migration stays the same whatever the app becomes.
"""
import re
from datetime import date

import sqlalchemy as sa
from alembic import op

revision = '0060'
down_revision = '0059'
branch_labels = None
depends_on = None

_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')  # date.fromisoformat alone also takes "20261105" and "2026-W45-4"
_TIME = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')

events = sa.table('events', sa.column('id', sa.Integer), sa.column('performances', sa.JSON))
runs = sa.table('event_run_dates', sa.column('id', sa.Integer), sa.column('event_id', sa.Integer),
                sa.column('date', sa.Date), sa.column('time', sa.String), sa.column('end_time', sa.String),
                sa.column('origin', sa.String))


def _normalise(performances) -> tuple[list[tuple[date, str]], int]:
    """The distinct (day, time) shows a performances list holds, sorted, and how many entries had no readable date.
    A time that isn't HH:MM ("evening", null) is ""; an untimed entry goes when the same day has a timed one."""
    if not isinstance(performances, list):
        return [], 0
    seen, bad = set(), 0
    for p in performances:
        raw = p.get('date') if isinstance(p, dict) else None
        try:
            day = date.fromisoformat(raw) if isinstance(raw, str) and _DAY.match(raw) else None
        except ValueError:
            day = None
        if day is None:
            bad += 1
            continue
        t = p.get('time')
        seen.add((day, t if isinstance(t, str) and _TIME.match(t) else ''))
    timed = {d for d, t in seen if t}
    return sorted((d, t) for d, t in seen if t or d not in timed), bad


def upgrade() -> None:
    # 1. Every row that exists was entered in a form
    op.add_column('event_run_dates', sa.Column('origin', sa.String(6), nullable=False, server_default='admin'))
    # 2. A matinee and an evening on one day
    op.drop_constraint('uq_event_run_dates_day', 'event_run_dates', type_='unique')
    op.create_unique_constraint('uq_event_run_dates_show', 'event_run_dates', ['event_id', 'date', 'time'])
    # 3. Copy the found dates. An event that already has a fixed run keeps only that run, as schedule() did.
    bind = op.get_bind()
    have = {row[0] for row in bind.execute(sa.select(runs.c.event_id).distinct())}
    copied_events = copied_shows = skipped = 0
    for event_id, performances in bind.execute(sa.select(events.c.id, events.c.performances).order_by(events.c.id)).all():
        if event_id in have or not performances:
            continue
        shows, bad = _normalise(performances)
        skipped += bad
        if shows:
            bind.execute(runs.insert(), [{'event_id': event_id, 'date': d, 'time': t, 'end_time': '', 'origin': 'found'}
                                         for d, t in shows])
            copied_events += 1
            copied_shows += len(shows)
    print(f"0060: copied {copied_shows} shows for {copied_events} events; skipped {skipped} entries with no readable date")


def downgrade() -> None:
    bind = op.get_bind()
    # 1. The found rows go back to events.performances, then go. Where the column still reads the same shows (it was
    #    frozen, so normally it does) it is left exactly as it was.
    by_event: dict[int, list[tuple[date, str]]] = {}
    for event_id, day, t in bind.execute(sa.select(runs.c.event_id, runs.c.date, runs.c.time).where(runs.c.origin == 'found')
                                         .order_by(runs.c.event_id, runs.c.date, runs.c.time)):
        by_event.setdefault(event_id, []).append((day, t))
    for event_id, shows in by_event.items():
        current = bind.execute(sa.select(events.c.performances).where(events.c.id == event_id)).scalar()
        if _normalise(current)[0] != shows:
            bind.execute(events.update().where(events.c.id == event_id)
                         .values(performances=[{'date': d.isoformat(), 'time': t or None} for d, t in shows]))
    op.execute("DELETE FROM event_run_dates WHERE origin = 'found'")
    # 2. One timed show a day, the earliest: what the old table could hold
    op.execute("DELETE FROM event_run_dates WHERE time = ''")
    op.execute("DELETE FROM event_run_dates a USING event_run_dates b "
               "WHERE a.event_id = b.event_id AND a.date = b.date AND a.time > b.time")
    # 3. The old constraint
    op.drop_constraint('uq_event_run_dates_show', 'event_run_dates', type_='unique')
    op.create_unique_constraint('uq_event_run_dates_day', 'event_run_dates', ['event_id', 'date'])
    op.drop_column('event_run_dates', 'origin')
