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
        {"id": "a2", "title": "Show", "venue": "The Second City", "begin": None, "end": "2030-03-01",
         "openRun": False, "beginTbd": True, "hood": "Old Town", "area": "City", "cost": "TBD", "min": None,
         "type": "Comedy", "url": "https://secondcity.example/show"},
        {"id": "a3", "title": "Show", "venue": "The Second City", "begin": None, "end": "2030-03-01",
         "openRun": False, "beginTbd": True, "hood": "Gold Coast", "area": "City", "cost": "Free", "min": 0,
         "type": "Comedy", "url": "https://secondcity.example/show2"},
        {"id": "bad", "title": "Backwards", "venue": "X", "begin": "2030-02-01", "end": "2030-01-01",
         "openRun": False, "beginTbd": False, "hood": "Loop", "area": "City", "cost": "", "min": None,
         "type": "Theater", "url": ""},
    ]
    path = tmp_path / "events.json"
    path.write_text(json.dumps({"events": rows}))
    monkeypatch.setattr(seed_module, "SessionLocal", Session)

    assert seed_module.seed(path) == {"added": 3, "updated": 0, "skipped": 1, "venues_added": 3}
    rows[0]["title"] = "Hamlet (extended)"
    path.write_text(json.dumps({"events": rows}))
    assert seed_module.seed(path) == {"added": 0, "updated": 3, "skipped": 1, "venues_added": 0}

    assert db.scalar(select(func.count(Event.id))) == 3
    assert db.scalar(select(func.count(Venue.id))) == 3  # Second City in two neighborhoods = two venues
    hamlet = db.scalar(select(Event).where(Event.source_id == "a1"))
    assert hamlet.title == "Hamlet (extended)" and hamlet.status == EventStatus.approved


@pytest.mark.skipif(not IS_POSTGRES, reason="migrations target Postgres; set TEST_DATABASE_URL to run")
def test_migrations_match_models(engine):
    with engine.begin() as conn:  # start from an empty database
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
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
