"""Test setup.

By default tests use an in-memory SQLite database, so they need nothing installed.
To run them against real Postgres (what production uses):

    TEST_DATABASE_URL=postgresql+psycopg://culture:culture@localhost:5432/culture_test pytest
"""
import os

os.environ.setdefault("ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Area, Event, EventStatus, Role, User, Venue  # noqa: E402
from app.ratelimit import auth_limiter  # noqa: E402

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")
IS_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")


@pytest.fixture(scope="session")
def engine():
    if IS_POSTGRES:
        eng = create_engine(TEST_DATABASE_URL)
    else:
        eng = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")  # SQLite ignores foreign keys unless asked
    yield eng
    eng.dispose()


@pytest.fixture
def Session(engine):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.drop_all(engine)


@pytest.fixture
def db(Session):
    with Session() as s:
        yield s


@pytest.fixture
def client(Session):
    def _get_db():
        with Session() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    auth_limiter.reset()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- helpers -----------------------------------------------------------------

def register(client, email="ana@example.com", password="correct-horse-1", name="Ana"):
    r = client.post("/auth/register", json={"email": email, "password": password, "display_name": name})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user_token(client):
    return register(client)


@pytest.fixture
def admin_token(client, db):
    token = register(client, email="admin@example.com", name="Admin")
    user = db.scalar(select(User).where(User.email == "admin@example.com"))
    user.role = Role.admin
    db.commit()
    return token


_DEFAULT = object()


@pytest.fixture
def make_event(db):
    """Insert an event directly. Defaults to an approved city theater show running now."""
    def _make(title="Hamlet", status=EventStatus.approved, venue_name="Goodman Theatre",
              neighborhood="Loop", area=Area.city, category="Theater",
              begin=_DEFAULT, end=_DEFAULT, min_price=None, cost_text=""):
        today = date.today()
        venue = db.scalar(select(Venue).where(Venue.name == venue_name, Venue.neighborhood == neighborhood))
        if venue is None:
            venue = Venue(name=venue_name, neighborhood=neighborhood, area=area)
            db.add(venue)
            db.flush()
        ev = Event(title=title, venue_id=venue.id, status=status, category=category,
                   begin_date=today - timedelta(days=5) if begin is _DEFAULT else begin,
                   end_date=today + timedelta(days=30) if end is _DEFAULT else end,
                   min_price=min_price, cost_text=cost_text, url="https://example.org/" + title.lower())
        db.add(ev)
        db.commit()
        return ev
    return _make
