"""CC-200: the API's database role (culture_app) can change rows but not the schema. Postgres only."""
import os

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from app.db import Base
from tests.conftest import IS_POSTGRES, TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="roles and grants are Postgres only; set TEST_DATABASE_URL")

# The same throwaway password docker-compose.yml uses, so these tests never break a local stack that shares the server.
APP_PASSWORD = "local-app-only"


@pytest.fixture
def migrated():
    """An empty database brought to head by the owner, torn down afterwards."""
    eng = create_engine(TEST_DATABASE_URL)
    with eng.begin() as conn:
        Base.metadata.drop_all(conn)
        for t in ("alembic_version", "sheet_syncs", "cc200_later"):
            conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
        for t in ("event_status", "venue_area", "user_role"):
            conn.execute(text(f"DROP TYPE IF EXISTS {t}"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("%", "%%"))
    command.upgrade(cfg, "head")
    try:
        yield eng
    finally:
        with eng.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS cc200_later"))
        command.downgrade(cfg, "base")
        eng.dispose()


def refused(conn, sql):
    """True if the statement is refused for lack of rights (rolled back to a savepoint so the test goes on)."""
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql))
    except ProgrammingError as e:
        sp.rollback()
        return "permission denied" in str(e) or "must be owner" in str(e)
    sp.rollback()
    return False


def test_app_role_can_change_rows_but_not_the_schema(migrated):
    with migrated.connect() as conn:
        conn.execute(text("SET ROLE culture_app"))
        conn.execute(text("INSERT INTO venues (name, neighborhood, area) VALUES ('CC-200 venue', '', 'city')"))  # needs the sequence
        assert conn.execute(text("SELECT count(*) FROM venues WHERE name = 'CC-200 venue'")).scalar() == 1
        conn.execute(text("UPDATE venues SET name = 'CC-200 renamed' WHERE name = 'CC-200 venue'"))
        conn.execute(text("DELETE FROM venues WHERE name = 'CC-200 renamed'"))
        head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
        for sql in ("UPDATE alembic_version SET version_num = '0001'",
                    "CREATE TABLE cc200_nope (id int)",
                    "ALTER TABLE venues ADD COLUMN cc200_nope int",
                    "DROP TABLE venues",
                    "TRUNCATE venues"):
            assert refused(conn, sql), sql
        conn.rollback()


def test_tables_created_by_later_migrations_are_covered(migrated):
    with migrated.begin() as conn:  # the owner, as a migration would
        conn.execute(text("CREATE TABLE cc200_later (id serial primary key, note text)"))
    with migrated.connect() as conn:
        conn.execute(text("SET ROLE culture_app"))
        conn.execute(text("INSERT INTO cc200_later (note) VALUES ('x')"))
        conn.rollback()


def _app_url():
    return make_url(TEST_DATABASE_URL).set(username="culture_app", password=APP_PASSWORD).render_as_string(hide_password=False)


def test_migrate_script_gives_the_app_role_a_login(migrated):
    from scripts import migrate
    assert migrate.set_app_password(TEST_DATABASE_URL, _app_url()) == "culture_app"
    assert migrate.check_rights(TEST_DATABASE_URL, "culture_app") == []
    app_engine = create_engine(_app_url())
    try:
        with app_engine.connect() as conn:
            assert conn.execute(text("SELECT current_user")).scalar() == "culture_app"
            assert conn.execute(text("SELECT count(*) FROM venues")).scalar() == 0
    finally:
        app_engine.dispose()


def test_migrate_script_names_a_table_the_role_cannot_use(migrated):
    from scripts import migrate
    with migrated.begin() as conn:
        conn.execute(text("REVOKE DELETE ON venues FROM culture_app"))
    problems = migrate.check_rights(TEST_DATABASE_URL, "culture_app")
    assert problems == ["culture_app lacks DELETE on venues"]


def test_migrate_script_main_reports_and_fails_without_both_urls(monkeypatch):
    from scripts import migrate
    monkeypatch.delenv("OWNER_DATABASE_URL", raising=False)
    assert migrate.main() == 2


def test_migrate_script_main_migrates_as_the_owner_even_with_the_app_url_set(migrated, monkeypatch):
    """The whole point: DATABASE_URL (the app role) is in the environment, but migrations must use the owner URL."""
    monkeypatch.setenv("OWNER_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", _app_url())
    from scripts import migrate
    assert migrate.main() == 0
