from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from ..deps import DB, AdminUser
from ..models import DiscoveredItem, DiscoveryCheck, DiscoverySource
from ..schemas import DiscoveryOverview, DiscoverySourceOut, DiscoverySourcePatch

router = APIRouter(prefix="/admin/discovery", tags=["discovery"])

# Claude Haiku 5.5 list price per million tokens (prompts up to 100K tokens), for an estimate only.
HAIKU_IN, HAIKU_OUT = 0.10, 0.50


def _source(db: DB, source_id: int) -> DiscoverySource:
    source = db.get(DiscoverySource, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    return source


def _out(source: DiscoverySource, waiting: dict, checks: dict) -> DiscoverySourceOut:
    out = DiscoverySourceOut.model_validate(source)
    out.kind = source.kind.value
    out.waiting = waiting.get(source.id, 0)
    out.checks_30d, out.pages_read_30d = checks.get(source.id, (0, 0))
    return out


@router.get("/sources", response_model=DiscoveryOverview)
def sources(db: DB, _: AdminUser):
    """Every event source (CC-70): its pages, how often it's checked, what the last check found, how
    many found items are waiting for review, and the last 30 days' model use."""
    since = datetime.now(UTC) - timedelta(days=30)
    waiting = dict(db.execute(select(DiscoveredItem.source_id, func.count())
                              .where(DiscoveredItem.status.in_(["new", "update"])).group_by(DiscoveredItem.source_id)).all())
    rows = db.execute(select(DiscoveryCheck.source_id, func.count(), func.coalesce(func.sum(DiscoveryCheck.pages_read), 0))
                      .where(DiscoveryCheck.checked_at >= since).group_by(DiscoveryCheck.source_id)).all()
    checks = {sid: (n, int(pages)) for sid, n, pages in rows}
    tin, tout = db.execute(select(func.coalesce(func.sum(DiscoveryCheck.input_tokens), 0),
                                  func.coalesce(func.sum(DiscoveryCheck.output_tokens), 0))
                           .where(DiscoveryCheck.checked_at >= since)).one()
    listed = db.scalars(select(DiscoverySource).order_by(func.lower(DiscoverySource.name))).all()
    return DiscoveryOverview(sources=[_out(s, waiting, checks) for s in listed], tokens_in_30d=int(tin), tokens_out_30d=int(tout),
                             cost_30d_usd=round(int(tin) / 1e6 * HAIKU_IN + int(tout) / 1e6 * HAIKU_OUT, 4))


@router.patch("/sources/{source_id}", response_model=DiscoverySourceOut)
def update_source(source_id: int, body: DiscoverySourcePatch, db: DB, _: AdminUser):
    """Change how often a source is checked, turn it off or on, or fix its page addresses."""
    source = _source(db, source_id)
    changes = body.model_dump(exclude_unset=True)
    if "every_days" in changes and changes["every_days"] is not None:
        source.every_days = changes["every_days"]
        if source.last_checked_at:  # the new gap counts from the last check
            source.next_check_on = source.last_checked_at.date() + timedelta(days=source.every_days)
    if changes.get("active") is not None:
        source.active = changes["active"]
    if changes.get("pages"):
        source.pages, source.page_state, source.next_check_on = changes["pages"], {}, None  # new pages: read them soon
    db.commit()
    return _out(source, {}, {})


@router.post("/sources/{source_id}/check", response_model=DiscoverySourceOut)
def check_now(source_id: int, db: DB, _: AdminUser):
    """Check this source at the next hourly run (e.g. after a season announcement)."""
    source = _source(db, source_id)
    source.next_check_on, source.unchanged_streak = None, 0
    db.commit()
    return _out(source, {}, {})
