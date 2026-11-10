from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .bodylimit import BodyLimit
from .config import get_settings
from .deps import DB
from .db import SessionLocal, engine
from .fromlink import warm_reader
from .health import Recorder, RequestRecorder
from .routers import admin, approvals, auth, crews, discovery, events, issues, journal, me, photos, seo, traffic, venues

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    warm_reader()  # CC-216
    # CC-244: the Site health page's numbers. Tests write them directly, so they don't run the thread.
    recorder = Recorder(engine, SessionLocal) if settings.env != "test" else None
    if recorder:
        recorder.start()
    yield
    if recorder:
        recorder.stop()


app = FastAPI(
    lifespan=lifespan,
    title="Culture Crew API",
    version="0.1.0",
    description="Chicago arts events, accounts, and event submissions for chiculturecrew.com.",
)

app.add_middleware(BodyLimit)  # CC-190: added first, so it runs inside CORS and the site can read its 413
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,  # lets the browser send the refresh cookie to /auth/refresh
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(RequestRecorder)  # CC-244: added last, so it's outermost and sees what the client got

for r in (auth.router, events.router, venues.router, me.router, journal.router, admin.router, traffic.router, discovery.router, crews.router, issues.router, photos.router, approvals.router, seo.router):
    app.include_router(r)


@app.get("/health", tags=["ops"])
def health(db: DB):
    """Used by the load balancer / uptime checks: is the API up and can it reach the database?"""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unreachable")
    return {"status": "ok"}
