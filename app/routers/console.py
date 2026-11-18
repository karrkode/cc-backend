import hmac
from datetime import date

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from .. import keys
from ..consolestats import check_week, last_finished_monday, week_numbers
from ..clock import today_in_chicago
from ..deps import DB
from ..ratelimit import SlidingWindowLimiter

router = APIRouter(prefix="/console", tags=["console"])
limiter = SlidingWindowLimiter(60.0)
PER_MINUTE = 10  # per address, right or wrong key: the console calls once a week


def _refuse(code: int) -> HTTPException:
    return HTTPException(code, headers={"Cache-Control": "no-store"})  # no detail: it says nothing about why


@router.get("/weekly")
def weekly(request: Request, response: Response, db: DB, week: str | None = None, x_console_key: str | None = Header(default=None)):
    """CC-240: one finished Monday-Sunday week's numbers for the Growth Console (counts only). It takes only the
    console's own key in the X-Console-Key header, not a sign-in, and that key opens nothing else."""
    address = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()  # set by Caddy, the only way in
               or (request.client.host if request.client else ""))
    if not limiter.hit(address, PER_MINUTE):
        raise _refuse(status.HTTP_429_TOO_MANY_REQUESTS)
    key = keys.console_key()
    given = (x_console_key or "").encode()
    # compare_digest takes bytes of any length; with no key set it still runs, so timing doesn't show that
    if not hmac.compare_digest(given, (key or "").encode()) or not key:
        raise _refuse(status.HTTP_401_UNAUTHORIZED)
    today = today_in_chicago()
    try:  # a str, not a date: a bad value must not be answered (422) before the key is checked
        monday = date.fromisoformat(week) if week else last_finished_monday(today)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "week must be a date like 2026-11-09") from None
    problem = check_week(monday, today)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
    response.headers["Cache-Control"] = "no-store"
    return week_numbers(db, monday)
