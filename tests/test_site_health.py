"""CC-244: the API notes its own requests and database checks for the admin Site health page."""
import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from app import health
from app.models import DbCheck, RequestMetric

from .conftest import auth


def get_health(client, token, hours=24):
    r = client.get(f"/admin/health?hours={hours}", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_members_get_403_and_strangers_401(client, user_token):
    assert client.get("/admin/health", headers=auth(user_token)).status_code == 403
    assert client.get("/admin/health").status_code == 401


def test_only_the_listed_windows(client, admin_token):
    assert client.get("/admin/health?hours=5", headers=auth(admin_token)).status_code == 422


def test_requests_are_noted_by_route_pattern_never_the_path(client, admin_token, make_event):
    e = make_event()
    assert client.get(f"/events/{e.id}?secret=abc").status_code == 200
    assert client.get("/no/such/place/ana@example.com").status_code == 404
    client.options("/events", headers={"Origin": "http://localhost:4173", "Access-Control-Request-Method": "GET"})
    h = get_health(client, admin_token)
    routes = {(r["method"], r["route"]) for r in h["timing"]["slowest"]}
    assert ("GET", "/events/{event_id}") in routes and ("GET", health.NO_ROUTE) in routes
    assert not any("secret" in r or "@" in r for _, r in routes)
    assert not any(m == "OPTIONS" for m, _ in routes)
    assert {"status": 404, "count": 1} in h["non_200"]
    assert all(s["status"] != 200 for s in h["non_200"])


def test_the_summary_numbers(client, admin_token, db):
    now = health.utcnow()
    for ms in range(1, 101):  # 1..100 ms, all 200s
        health.note("GET", "/events", 200, float(ms), at=now - timedelta(minutes=5))
    health.note("POST", "/auth/login", 401, 50.0, at=now - timedelta(minutes=5))
    health.note("POST", "/auth/login", 401, 50.0, at=now - timedelta(minutes=5))
    health.note("GET", "/venues", 503, 900.0, at=now - timedelta(hours=2))
    health.note("GET", "/old", 500, 5.0, at=now - timedelta(days=2))  # outside the day
    health.note_check(True, 2.0, at=now - timedelta(minutes=3))
    health.note_check(False, None, at=now - timedelta(hours=3))
    h = get_health(client, admin_token)
    t = h["timing"]
    assert 50 <= t["p50_ms"] <= 52 and 94 <= t["p95_ms"] <= 97 and t["p99_ms"] >= 98
    assert t["slowest"][0]["route"] == "/venues" and t["slowest"][0]["max_ms"] == 900.0
    assert h["non_200"][0] == {"status": 401, "count": 2} and {"status": 503, "count": 1} in h["non_200"]
    api = h["api"]
    assert api["server_errors"] == 1 and api["recent_errors"][0]["route"] == "/venues"
    assert api["errors_last_15_min"] == 0
    assert h["db"]["ok"] is True and h["db"]["checks"] == 2 and h["db"]["failed"] == 1 and h["db"]["avg_ms"] == 2.0
    assert h["db"]["last_failed_at"] and h["state"] == "healthy"  # the failure was three hours ago
    week = get_health(client, admin_token, hours=168)
    assert week["api"]["server_errors"] == 2


def test_a_recent_server_error_is_trouble(client, admin_token):
    health.note("GET", "/events", 500, 10.0)
    assert get_health(client, admin_token)["state"] == "trouble"


def test_a_recent_failed_db_check_is_trouble(client, admin_token):
    health.note_check(False, None)
    assert get_health(client, admin_token)["state"] == "trouble"


def test_a_crash_is_noted_as_500_and_still_raised():
    async def boom(scope, receive, send):
        raise RuntimeError("boom")
    health.reset()
    scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}
    try:
        asyncio.run(health.RequestRecorder(boom)(scope, None, None))
    except RuntimeError:
        pass
    else:
        raise AssertionError("the error should go on to the server")
    assert list(health._requests)[0][1:4] == ("GET", health.NO_ROUTE, 500)


def test_noting_never_breaks_a_request(client, monkeypatch):
    def broken(*a, **k):
        raise ValueError("nope")
    monkeypatch.setattr(health, "note", broken)
    assert client.get("/health").status_code == 200


def test_old_rows_are_trimmed(db):
    now = health.utcnow()
    health.reset()
    health.note("GET", "/a", 200, 1.0, at=now - timedelta(days=31))
    health.note("GET", "/b", 200, 1.0, at=now - timedelta(days=29))
    health.note_check(True, 1.0, at=now - timedelta(days=31))
    health.flush(db)
    health.trim(db, now)
    assert [r.route for r in db.scalars(select(RequestMetric))] == ["/b"]
    assert db.scalar(select(func.count(DbCheck.id))) == 0


def test_the_recorder_tick_checks_and_writes(Session, engine):
    health.reset()
    health.note("GET", "/a", 200, 1.0)
    health.Recorder(engine, Session).tick()
    with Session() as s:
        assert s.scalar(select(func.count(RequestMetric.id))) == 1
        assert s.scalar(select(DbCheck.ok)) is True


def test_failed_checks_wait_for_the_database(monkeypatch, db):
    health.reset()
    health.note_check(False, None)
    def down(*a, **k):
        raise RuntimeError("database down")
    monkeypatch.setattr(db, "execute", down)
    health.flush(db)
    assert len(health._checks) == 1
    monkeypatch.undo()
    health.flush(db)
    assert len(health._checks) == 0 and db.scalar(select(func.count(DbCheck.id))) == 1
