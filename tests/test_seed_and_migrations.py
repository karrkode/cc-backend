import json

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, select, text

from app.areas import area_of
from app.db import Base
from app.models import Area, Event, EventStatus, Venue
from scripts import seed as seed_module

from .conftest import IS_POSTGRES, TEST_DATABASE_URL


def test_area_rules():
    assert area_of("Lincoln Park") == Area.city
    assert area_of("Evanston, IL") == Area.suburbs
    assert area_of("Niles") == Area.suburbs
    assert area_of("Rockford, IL") == Area.out_of_town
    assert area_of("Milwaukee, WI") == Area.out_of_town


def test_seed_is_repeatable(Session, db, monkeypatch, tmp_path):
    rows = [
        {"id": "a1", "title": "Hamlet", "venue": "Goodman Theatre", "begin": "2030-01-01", "end": "2030-02-01",
         "openRun": False, "beginTbd": False, "hood": "Loop", "area": "City", "cost": "$35+", "min": 35.0,
         "type": "Theater", "url": "https://goodman.example/hamlet"},
        {"id": "a2", "title": "Show", "venue": "Lakeview Comedy Club", "begin": None, "end": "2030-03-01",
         "openRun": False, "beginTbd": True, "hood": "Lakeview", "area": "City", "cost": "TBD", "min": None,
         "type": "Comedy", "url": "https://comedyclub.example/show"},
        {"id": "a3", "title": "Show", "venue": "Lakeview Comedy Club", "begin": None, "end": "2030-03-01",
         "openRun": False, "beginTbd": True, "hood": "Old Town", "area": "City", "cost": "Free", "min": 0,
         "type": "Comedy", "url": "https://comedyclub.example/show2"},
        {"id": "bad", "title": "Backwards", "venue": "X", "begin": "2030-02-01", "end": "2030-01-01",
         "openRun": False, "beginTbd": False, "hood": "Loop", "area": "City", "cost": "", "min": None,
         "type": "Theater", "url": ""},
    ]
    path = tmp_path / "events.json"
    path.write_text(json.dumps({"events": rows}))
    monkeypatch.setattr(seed_module, "SessionLocal", Session)

    first = seed_module.seed(path)
    assert (first["added"], first["updated"], first["skipped"], first["venues_added"]) == (3, 0, 1, 3)
    rows[0]["title"] = "Hamlet (extended)"
    path.write_text(json.dumps({"events": rows}))
    again = seed_module.seed(path)
    assert (again["added"], again["updated"], again["unchanged"], again["venues_added"]) == (0, 1, 2, 0)

    assert db.scalar(select(func.count(Event.id))) == 3
    assert db.scalar(select(func.count(Venue.id))) == 3  # the comedy club in two neighborhoods = two venues
    hamlet = db.scalar(select(Event).where(Event.source_id == "a1"))
    assert hamlet.title == "Hamlet (extended)" and hamlet.status == EventStatus.approved


def test_migrations_have_one_head():
    """CC-265: two stories that both took the next number leave two heads, and `alembic upgrade head` then fails
    when the container starts. This needs no database, so it runs on SQLite too."""
    from alembic.script import ScriptDirectory
    assert len(ScriptDirectory.from_config(Config("alembic.ini")).get_heads()) == 1


@pytest.mark.skipif(not IS_POSTGRES, reason="migrations target Postgres; set TEST_DATABASE_URL to run")
def test_migrations_match_models(engine):
    with engine.begin() as conn:  # start from an empty database
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        conn.execute(text("DROP TABLE IF EXISTS sheet_syncs"))
        for t in ("event_status", "venue_area", "user_role"):
            conn.execute(text(f"DROP TYPE IF EXISTS {t}"))

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    try:
        with create_engine(TEST_DATABASE_URL).connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == [], f"models and migrations disagree; run `alembic revision --autogenerate`: {diff}"
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")  # downgrade leaves nothing behind that blocks a re-run
    finally:
        command.downgrade(cfg, "base")


@pytest.mark.skipif(not IS_POSTGRES, reason="migrations target Postgres; set TEST_DATABASE_URL to run")
def test_cc125_migration_maps_old_types_and_fills_venue_defaults(engine):
    with engine.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for (t,) in conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).all():
            conn.execute(text(f'DROP TYPE IF EXISTS "{t}" CASCADE'))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "0032")
    try:
        with create_engine(TEST_DATABASE_URL).begin() as conn:
            conn.execute(text("INSERT INTO venues (id, name, neighborhood, area) VALUES (1, 'Metro', 'Wrigleyville', 'city'), "
                              "(2, 'Goodman', 'Loop', 'city'), (3, 'Empty', 'Loop', 'city')"))
            for i, (v, cat) in enumerate([(1, "Concert"), (1, "Concert"), (1, "Comedy"), (2, "Theater"), (2, "Dance")]):
                conn.execute(text("INSERT INTO events (id, title, sort_title, venue_id, category, status, open_run, cost_text, url) "
                                  "VALUES (:i, :t, :t, :v, :c, 'approved', false, '', '')"), {"i": i + 1, "t": f"e{i}", "v": v, "c": cat})
        command.upgrade(cfg, "0033")
        with create_engine(TEST_DATABASE_URL).connect() as conn:
            cats = dict(conn.execute(text("SELECT title, category FROM events")).all())
            defaults = dict(conn.execute(text("SELECT name, category FROM venues")).all())
        assert cats == {"e0": "Music", "e1": "Music", "e2": "Comedy", "e3": "Theater", "e4": "Dance"}  # Dance is left for an admin
        assert defaults == {"Metro": "Music", "Goodman": "Theater", "Empty": None}
        command.downgrade(cfg, "0032")
        with create_engine(TEST_DATABASE_URL).connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM events WHERE category = 'Concert'")).scalar() == 2
    finally:
        command.downgrade(cfg, "base")


def _dates_of(performances, run_rows, today):
    """What Event.schedule's dates were before CC-301, for a clean list: a fixed run's own dates, else the performances."""
    given = [{"date": d, "time": t} for d, t in run_rows]
    return sorted((p for p in given or performances or [] if p["date"] >= today), key=lambda p: (p["date"], p["time"] or ""))[:120]


@pytest.mark.skipif(not IS_POSTGRES, reason="migrations target Postgres; set TEST_DATABASE_URL to run")
def test_cc301_migration_copies_found_dates_with_real_data_shapes_and_downgrades(engine):
    """CC-301: 0060 copies events.performances into event_run_dates as found rows, with the shapes real data has."""
    from datetime import timedelta
    from sqlalchemy.orm import sessionmaker
    from app.stats import today_in_chicago
    from scripts.show_run_report import report

    with engine.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for (t,) in conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).all():
            conn.execute(text(f'DROP TYPE IF EXISTS "{t}" CASCADE'))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "0059")
    today = today_in_chicago()
    day = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    P = lambda n, t="19:30": {"date": day(n), "time": t}  # noqa: E731
    perf = {
        1: ("timed", [P(1), P(2, "20:00")]),
        2: ("null time", [P(1, None), P(2, None)]),
        3: ("evening", [P(1, "evening"), P(2, "20:00"), P(3, "25:99")]),
        4: ("bad dates", ["junk", {"time": "19:30"}, {"date": None}, {"date": "soon", "time": "19:30"}, {"date": "2026-02-30"},
                          {"date": "20261105", "time": "19:30"}, P(4)]),
        5: ("matinee and evening", [P(1, "20:00"), P(1, "14:00")]),
        6: ("repeated", [P(1), P(1), P(2, None), P(2, None)]),
        7: ("untimed and timed one day", [P(1, None), P(1, "19:30"), P(2, None)]),
        8: ("fixed run and performances", [P(5), P(6)]),
        9: ("open run", [P(1), P(8)]),
        10: ("visual art talk", [P(3, "18:00"), P(10, "18:00")]),
        11: ("forty-five", [P(n) for n in range(1, 46)]),
        12: ("past dates", [P(-30), P(-2), P(3)]),
        13: ("none", []),
        14: ("json null", None),
    }
    with create_engine(TEST_DATABASE_URL).begin() as conn:
        conn.execute(text("INSERT INTO venues (id, name, neighborhood, area) VALUES (1, 'Hall', 'Loop', 'city')"))
        for i, (title, p) in perf.items():
            conn.execute(text("INSERT INTO events (id, title, sort_title, venue_id, category, status, open_run, cost_text, url, performances) "
                              "VALUES (:i, :t, :t, 1, :c, 'approved', :o, '', '', CAST(:p AS json))"),
                         {"i": i, "t": title, "c": "Visual Art" if i == 10 else "Theater", "o": i == 9, "p": json.dumps(p)})
        conn.execute(text("INSERT INTO event_run_dates (event_id, date, time, end_time) VALUES (8, :a, '19:00', ''), (8, :b, '19:00', '21:00')"),
                     {"a": day(2), "b": day(9)})
    snapshot = "SELECT * FROM events ORDER BY id"
    with create_engine(TEST_DATABASE_URL).connect() as conn:
        events_before = [tuple(r) for r in conn.execute(text(snapshot))]
    Session = sessionmaker(bind=create_engine(TEST_DATABASE_URL), expire_on_commit=False)
    with Session() as db:
        before = report(db)
    assert "not applied yet" in before[0]
    # copied: every event with performances, no fixed run, and a readable date (1-7, 9-12), not the fixed run (8)
    assert "Events whose dates will be copied: 11 (" in before[1] and "skipped because they already have a fixed run: 1" in before[-1]

    try:
        command.upgrade(cfg, "0060")
        with create_engine(TEST_DATABASE_URL).connect() as conn:
            assert [tuple(r) for r in conn.execute(text(snapshot))] == events_before  # the events rows are untouched
            rows = {}
            for e, d, t, end, origin in conn.execute(text("SELECT event_id, date, time, end_time, origin FROM event_run_dates ORDER BY event_id, date, time")):
                rows.setdefault(e, []).append((d.isoformat(), t, end, origin))
        F = lambda n, t="19:30": (day(n), t, "", "found")  # noqa: E731
        assert rows == {
            1: [F(1), F(2, "20:00")],
            2: [F(1, ""), F(2, "")],  # null becomes "not posted"
            3: [F(1, ""), F(2, "20:00"), F(3, "")],  # "evening" and 25:99 too; an unreadable time is kept as a show with none
            4: [F(4)],  # only the one readable date is copied
            5: [F(1, "14:00"), F(1, "20:00")],  # a matinee and an evening both stay
            6: [F(1), F(2, "")],  # repeats collapse
            7: [F(1), F(2, "")],  # the untimed entry yields to the timed one on its day
            8: [(day(2), "19:00", "", "admin"), (day(9), "19:00", "21:00", "admin")],  # the fixed run is alone, and keeps its origin
            9: [F(1), F(8)],
            10: [F(3, "18:00"), F(10, "18:00")],
            11: [F(n) for n in range(1, 46)],
            12: [F(-30), F(-2), F(3)],
        }
        # Find an outing sees the same upcoming dates on the events with clean data (the dirty ones are normalised)
        with Session() as db:
            for i in (1, 5, 8, 9, 10, 11, 12):
                old = _dates_of(perf[i][1], [(day(2), "19:00"), (day(9), "19:00")] if i == 8 else [], day(0))
                assert db.get(Event, i).schedule["dates"] == old, perf[i][0]
            assert db.get(Event, 3).schedule["dates"] == [{"date": day(1), "time": None}, {"date": day(2), "time": "20:00"}, {"date": day(3), "time": None}]
            assert db.get(Event, 8).schedule["dates_from"] == "event" and db.get(Event, 9).schedule["dates_from"] == "found"
            assert db.get(Event, 13).schedule["dates_from"] == "" and db.get(Event, 8).show_run_exact and not db.get(Event, 9).show_run_exact
            after = report(db)
        assert "migration: applied" in after[0] and "'found': " in after[1] and "'admin': 2" in after[1]
        assert any(line.startswith("  8  ") for line in after[3:])  # the worklist: listed with no dates, its first and last show are not

        # after the upgrade a person saves shows, and sourcing refreshes another event's: the downgrade has to cope
        with create_engine(TEST_DATABASE_URL).begin() as conn:
            conn.execute(text("DELETE FROM event_run_dates WHERE event_id = 1 AND date = :d"), {"d": day(2)})
            conn.execute(text("INSERT INTO event_run_dates (event_id, date, time, origin) VALUES (1, :d, '21:00', 'found')"), {"d": day(7)})
            conn.execute(text("INSERT INTO event_run_dates (event_id, date, time, origin) VALUES "
                              "(13, :a, '20:00', 'admin'), (13, :a, '14:00', 'member'), (13, :b, '', 'admin')"), {"a": day(3), "b": day(4)})
        command.downgrade(cfg, "0059")
        with create_engine(TEST_DATABASE_URL).connect() as conn:
            assert "origin" not in {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'event_run_dates'"))}
            perfs = {i: p for i, p in conn.execute(text("SELECT id, performances FROM events ORDER BY id"))}
            kept = [(e, d.isoformat(), t, end) for e, d, t, end in conn.execute(text("SELECT event_id, date, time, end_time FROM event_run_dates ORDER BY event_id, date"))]
            events_after = [tuple(r) for r in conn.execute(text(snapshot))]
            with pytest.raises(Exception, match="uq_event_run_dates_day"):
                conn.execute(text("INSERT INTO event_run_dates (event_id, date, time) VALUES (8, :d, '10:00')"), {"d": day(2)})
        assert perfs[1] == [{"date": day(1), "time": "19:30"}, {"date": day(7), "time": "21:00"}]  # the refreshed shows came back
        assert [r for i, r in enumerate(events_after) if events_before[i][0] not in (1, 13)] == \
            [r for r in events_before if r[0] not in (1, 13)]  # everything else, junk included, is exactly as it was
        assert kept == [(8, day(2), "19:00", ""), (8, day(9), "19:00", "21:00"), (13, day(3), "14:00", "")]  # one timed show a day, the earliest
        command.upgrade(cfg, "0060")  # and it goes forward again
        with create_engine(TEST_DATABASE_URL).connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM event_run_dates WHERE origin = 'found'")).scalar() > 0
    finally:
        command.downgrade(cfg, "base")
