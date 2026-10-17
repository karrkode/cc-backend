from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .config import get_settings
from .deps import DB
from .routers import admin, auth, events, me, venues

settings = get_settings()

app = FastAPI(
    title="Culture Crew API",
    version="0.1.0",
    description="Chicago arts events, accounts, and event submissions for chiculturecrew.com.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,  # lets the browser send the refresh cookie to /auth/refresh
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

for r in (auth.router, events.router, venues.router, me.router, admin.router):
    app.include_router(r)


@app.get("/health", tags=["ops"])
def health(db: DB):
    """Used by the load balancer / uptime checks: is the API up and can it reach the database?"""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unreachable")
    return {"status": "ok"}
