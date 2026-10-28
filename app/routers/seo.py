"""CC-108: the event pages, sitemap and robots.txt (see app/seo.py). On chiculturecrew.com, CloudFront
sends /e/*, /sitemap.xml and /robots.txt here and caches them for CACHE_SECONDS."""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import or_, select

from ..deps import DB
from ..models import Event
from ..seo import id_from, path_for, render_event, render_missing, robots, sitemap
from ..stats import today_in_chicago
from ..takes import takes_for
from ..visibility import is_live, live_condition

router = APIRouter(tags=["seo"], include_in_schema=False)

CACHE_SECONDS = 600
CACHED = {"Cache-Control": f"public, max-age={CACHE_SECONDS}"}


@router.get("/e/{ref}", response_class=HTMLResponse)
def event_page(ref: str, db: DB):
    event_id = id_from(ref)
    event = db.get(Event, event_id) if event_id is not None else None
    if event is None or not is_live(event):
        return HTMLResponse(render_missing(), status_code=404, headers={"Cache-Control": "public, max-age=60"})
    canonical = path_for(event)
    if f"/e/{ref}" != canonical:  # an old title or a bare id: send it to the current address
        return RedirectResponse(canonical, status_code=301, headers=CACHED)
    return HTMLResponse(render_event(event, today_in_chicago(), takes_for(db, event.id)), headers=CACHED)


@router.get("/sitemap.xml")
def sitemap_xml(db: DB):
    today = today_in_chicago()
    events = db.scalars(select(Event).where(live_condition(), or_(Event.end_date.is_(None), Event.end_date >= today))
                        .order_by(Event.id)).all()
    return Response(sitemap(list(events), today), media_type="application/xml", headers=CACHED)


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return PlainTextResponse(robots(), headers=CACHED)
