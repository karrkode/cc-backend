"""CC-244: the API keeps its own health numbers for the admin Site health page, without CloudWatch.

Each request is noted in memory as it finishes (method, route pattern, status, milliseconds), which costs next to
nothing, and a background thread writes the notes to `request_metrics` in one batch a minute. The same thread checks
the database each minute (`db_checks`) and, once an hour, drops rows older than 30 days. A request never waits on
any of this, and a failure here is logged, never passed on to the request.

Only the route's pattern is kept (/events/{event_id}), never the real path, query, body, address or user. A request
no route matched (a scanner's probe) is kept as "(no route)", so made-up paths aren't stored either.

Uvicorn runs one worker (Dockerfile), so the notes in memory are the whole picture. AWS-level alarms are CC-192's.
"""
import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from .models import DbCheck, RequestMetric

log = logging.getLogger(__name__)

KEEP_DAYS = 30
WINDOWS = (1, 24, 168, 720)  # the page's choices, in hours: up to the 30 days kept
EVERY_S = 60  # how often the thread writes the notes and checks the database
TRIM_EVERY_S = 3600
NO_ROUTE = "(no route)"
SKIP_METHODS = ("OPTIONS", "HEAD")  # CORS preflights and probes say nothing about the API's own health
RECENT_S = 15 * 60  # a server error or failed database check this recent marks the API as having trouble

STARTED_AT = datetime.now(UTC)
# Bounded, so a database that's down for hours can't fill the memory: the oldest notes go first.
_requests: deque = deque(maxlen=50_000)
_checks: deque = deque(maxlen=2_000)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=UTC) if d is not None and d.tzinfo is None else d  # SQLite gives times back without a zone


# ---------- recording ----------

def note(method: str, route: str, status: int, ms: float, at: datetime | None = None) -> None:
    _requests.append((at or utcnow(), method[:8], route[:200], status, round(ms, 1)))


class RequestRecorder:
    """ASGI middleware, the outermost, so it sees what the client got (a CORS refusal or a 413 too).
    It only reads what passes: the status as the response starts, and the route FastAPI matched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in SKIP_METHODS:
            await self.app(scope, receive, send)
            return
        start, seen = time.perf_counter(), {"status": 500}  # an exception before the response starts is a 500

        async def watched(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, watched)
        finally:
            try:
                route = getattr(scope.get("route"), "path", None) or NO_ROUTE
                note(scope["method"], route, seen["status"], (time.perf_counter() - start) * 1000)
            except Exception:  # never let the counting break a request
                log.exception("CC-244: couldn't note a request")


def check_db(engine) -> tuple[bool, float | None]:
    """One timed SELECT 1 on its own connection."""
    start = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, round((time.perf_counter() - start) * 1000, 1)
    except Exception:
        log.warning("CC-244: the database didn't answer its health check", exc_info=True)
        return False, None


def note_check(ok: bool, ms: float | None, at: datetime | None = None) -> None:
    _checks.append((at or utcnow(), ok, ms))


def flush(db: Session) -> None:
    """Write what's been noted. Requests that fail to write are dropped (the failed check says why); checks are kept
    to try again, since a failed check is the one thing we most want to see once the database is back."""
    reqs, checks = [], []
    while _requests:
        reqs.append(_requests.popleft())
    while _checks:
        checks.append(_checks.popleft())
    if not reqs and not checks:
        return
    try:
        if reqs:
            db.execute(insert(RequestMetric), [{"at": r[0], "method": r[1], "route": r[2], "status": r[3], "ms": r[4]} for r in reqs])
        if checks:
            db.execute(insert(DbCheck), [{"at": c[0], "ok": c[1], "ms": c[2]} for c in checks])
        db.commit()
    except Exception:
        db.rollback()
        _checks.extendleft(reversed(checks))
        log.warning("CC-244: couldn't write %d request notes; dropped them", len(reqs), exc_info=True)


def trim(db: Session, now: datetime | None = None) -> None:
    cut = (now or utcnow()) - timedelta(days=KEEP_DAYS)
    db.execute(delete(RequestMetric).where(RequestMetric.at < cut))
    db.execute(delete(DbCheck).where(DbCheck.at < cut))
    db.commit()


def reset() -> None:
    """Tests: forget anything noted but not written."""
    _requests.clear(); _checks.clear()


# ---------- the background thread ----------

class Recorder:
    def __init__(self, engine, session_factory):
        self.engine, self.Session = engine, session_factory
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.trimmed_at = 0.0

    def tick(self) -> None:
        note_check(*check_db(self.engine))
        with self.Session() as db:
            flush(db)
            if time.monotonic() - self.trimmed_at >= TRIM_EVERY_S:
                trim(db)
                self.trimmed_at = time.monotonic()

    def run(self) -> None:
        while not self.stop_event.wait(EVERY_S):
            try:
                self.tick()
            except Exception:
                log.exception("CC-244: the health recorder's tick failed")

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="health-recorder", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:  # what's noted since the last tick, so a deploy doesn't lose the last minute
            with self.Session() as db:
                flush(db)
        except Exception:
            log.warning("CC-244: couldn't write the last request notes", exc_info=True)


# ---------- the summary for the page ----------

def _percentile(db: Session, since: datetime, n: int, q: float) -> float | None:
    """Nearest-rank percentile, done in the database so SQLite and Postgres agree and nothing big comes back."""
    if not n:
        return None
    at = min(n - 1, max(0, int(round(q * (n - 1)))))
    return db.scalar(select(RequestMetric.ms).where(RequestMetric.at >= since).order_by(RequestMetric.ms).offset(at).limit(1))


def _r(v) -> float | None:
    return None if v is None else round(float(v), 1)


def summary(db: Session, hours: int, now: datetime | None = None) -> dict:
    now = now or utcnow()
    since, recent = now - timedelta(hours=hours), now - timedelta(seconds=RECENT_S)
    in_window = RequestMetric.at >= since

    n, avg = db.execute(select(func.count(RequestMetric.id), func.avg(RequestMetric.ms)).where(in_window)).one()
    server_errors = db.scalar(select(func.count(RequestMetric.id)).where(in_window, RequestMetric.status >= 500)) or 0
    non_200 = db.execute(select(RequestMetric.status, func.count(RequestMetric.id)).where(in_window, RequestMetric.status != 200)
                         .group_by(RequestMetric.status).order_by(func.count(RequestMetric.id).desc(), RequestMetric.status)).all()
    errors = db.scalars(select(RequestMetric).where(in_window, RequestMetric.status >= 500)
                        .order_by(RequestMetric.at.desc(), RequestMetric.id.desc()).limit(10)).all()
    slow = db.execute(select(RequestMetric.method, RequestMetric.route, func.count(RequestMetric.id), func.avg(RequestMetric.ms),
                             func.max(RequestMetric.ms))
                      .where(in_window).group_by(RequestMetric.method, RequestMetric.route)
                      .order_by(func.avg(RequestMetric.ms).desc(), RequestMetric.route).limit(5)).all()
    errors_recent = db.scalar(select(func.count(RequestMetric.id)).where(RequestMetric.at >= recent, RequestMetric.status >= 500)) or 0

    checks_in = DbCheck.at >= since
    c_n, c_failed, c_avg = db.execute(select(func.count(DbCheck.id), func.count(DbCheck.id).filter(DbCheck.ok.is_(False)),
                                             func.avg(DbCheck.ms).filter(DbCheck.ok.is_(True))).where(checks_in)).one()
    last_failed = db.scalar(select(func.max(DbCheck.at)).where(DbCheck.ok.is_(False)))
    start = time.perf_counter()  # and one now, for the page (if this fails, so did everything above)
    db.execute(text("SELECT 1"))
    db_ok, db_ms = True, round((time.perf_counter() - start) * 1000, 1)

    db_trouble = not db_ok or (last_failed is not None and _aware(last_failed) >= recent)
    return {
        "hours": hours,
        "now": now,
        "state": "trouble" if db_trouble or errors_recent else "healthy",
        "api": {
            "started_at": STARTED_AT,
            "requests": n,
            "server_errors": server_errors,
            "errors_last_15_min": errors_recent,
            "recent_errors": [{"at": _aware(e.at), "method": e.method, "route": e.route, "status": e.status, "ms": _r(e.ms)} for e in errors],
        },
        "timing": {
            "avg_ms": _r(avg),
            "p50_ms": _r(_percentile(db, since, n, 0.50)),
            "p95_ms": _r(_percentile(db, since, n, 0.95)),
            "p99_ms": _r(_percentile(db, since, n, 0.99)),
            "slowest": [{"method": m, "route": r, "requests": c, "avg_ms": _r(a), "max_ms": _r(x)} for m, r, c, a, x in slow],
        },
        "db": {
            "ok": db_ok,
            "ms": db_ms,
            "checks": c_n,
            "failed": c_failed,
            "avg_ms": _r(c_avg),
            "last_failed_at": _aware(last_failed),
        },
        "non_200": [{"status": s, "count": c} for s, c in non_200],
    }
