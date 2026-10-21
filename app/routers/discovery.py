import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select

from ..deps import DB, AdminUser
from ..discovery.budget import budget_state, set_monthly_budget
from ..discovery.needs import needs
from ..discovery.readers import cost_usd
from ..discovery.rules import norm_title
from ..discovery.store import matching_rule, possible_duplicates, rule_reason, sync_performances, venue_for
from ..models import DiscoveredItem, DiscoveryCheck, DiscoverySource, Event, SkipRule, Venue
from ..schemas import (BudgetIn, DiscoveredItemOut, DiscoveredPage, DiscoveryOverview, DiscoverySourceOut, DiscoverySourcePatch,
                       EventIn, EventRef, FoundChange, RejectIn, RejectOut, SkipRuleOut, UnreadableSource)
from .events import new_event

router = APIRouter(prefix="/admin/discovery", tags=["discovery"])


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
    state, spent, budget = budget_state(db)
    return DiscoveryOverview(sources=[_out(s, waiting, checks) for s in listed], tokens_in_30d=int(tin), tokens_out_30d=int(tout),
                             cost_30d_usd=cost_usd(int(tin), int(tout)), spent_month_usd=spent, budget_usd=budget, budget_state=state)


@router.put("/budget", response_model=DiscoveryOverview)
def set_budget(body: BudgetIn, db: DB, admin: AdminUser):
    """Change the monthly budget for reading pages with Claude (CC-80)."""
    set_monthly_budget(db, round(body.usd, 2))
    db.commit()
    return sources(db, admin)


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
    if changes.get("manual") is not None:  # back to automatic: checked at the next hourly run
        source.manual = changes["manual"]
        if not source.manual:
            source.next_check_on = None
    if changes.get("pages"):
        source.pages, source.page_state, source.next_check_on = changes["pages"], {}, None  # new pages: read them soon
    db.commit()
    return _out(source, {}, {})


@router.post("/sources/{source_id}/check", response_model=DiscoverySourceOut)
def check_now(source_id: int, db: DB, _: AdminUser):
    """Check this source at the next hourly run (e.g. after a season announcement)."""
    source = _source(db, source_id)
    source.next_check_on, source.unchanged_streak, source.check_requested = None, 0, True  # runs even past the budget
    db.commit()
    return _out(source, {}, {})


# ---- review (CC-71) ---------------------------------------------------------------

Show = Literal["waiting", "needs", "new", "update", "approved", "rejected", "skipped", "listed", "all"]
SHOWS = {"waiting": ["new", "update"], "needs": ["new"], "all": None}  # needs: new ones missing a date or show days (CC-88)
Has = Literal["any", "listed", "none"]  # CC-82: has a price (or "Free") / has an opening or closing date


def _has(q, has: Has, condition):
    return q if has == "any" else q.where(condition if has == "listed" else ~condition)


def _narrowed(q, source: int | None, price: Has, dates: Has):
    """The filters besides status: source, price listed or not, dates listed or not."""
    if source is not None:
        q = q.where(DiscoveredItem.source_id == source)
    q = _has(q, price, (DiscoveredItem.cost_text != "") | DiscoveredItem.min_price.is_not(None))
    return _has(q, dates, DiscoveredItem.begin_date.is_not(None) | DiscoveredItem.end_date.is_not(None))


def _item(db: DB, item_id: int) -> DiscoveredItem:
    item = db.get(DiscoveredItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Found item not found")
    return item


def _filtered(show: str, source: int | None, price: Has = "any", dates: Has = "any"):
    q = select(DiscoveredItem)
    statuses = SHOWS.get(show, [show])
    if statuses:
        q = q.where(DiscoveredItem.status.in_(statuses))
    return _narrowed(q, source, price, dates)


def _item_out(db: DB, item: DiscoveredItem, dups: dict | None = None) -> DiscoveredItemOut:
    out = DiscoveredItemOut.model_validate(item)
    out.source_name = item.source.name
    venue = venue_for(db, item.source, item)
    if venue is not None:
        out.venue_id, out.neighborhood = venue.id, venue.neighborhood
        out.venue_name = item.venue_name or venue.name
    else:  # a venue we don't have yet: the source's own, with its neighborhood, or one the item names
        out.venue_name = item.venue_name or item.source.venue_name
        if not item.venue_name or norm_title(item.venue_name) == norm_title(item.source.venue_name):
            out.neighborhood = item.source.neighborhood
    out.dates_listed = len(item.performances or [])
    out.dates_timed = any(p.get("time") for p in item.performances or [])
    out.venue_show_times = venue.show_times if venue is not None else {}
    out.needs = needs(item, venue) if item.status == "new" else ""
    out.proposed = [FoundChange(field=k, listed=v[0], found=v[1]) for k, v in (item.changes or {}).items()] \
        if item.status == "update" else []
    event = db.get(Event, item.event_id) if item.event_id else None
    out.event = EventRef(id=event.id, title=event.title) if event else None
    dup = (dups or {}).get(item.id)
    out.possible_duplicate = EventRef(id=dup.id, title=dup.title) if dup else None
    return out


@router.get("/items", response_model=DiscoveredPage)
def items(db: DB, _: AdminUser, show: Show = "waiting", source: int | None = None, price: Has = "any", dates: Has = "any",
          limit: Annotated[int, Query(ge=1, le=500)] = 100, offset: Annotated[int, Query(ge=0)] = 0):
    """Found events for review, newest first: new events and proposed updates ("waiting"), or one
    status; optionally only those with (or without) a price, or dates. Also the count per status
    under the same filters, and the sources that couldn't be read, for a manual look."""
    q = _filtered(show, source, price, dates).order_by(DiscoveredItem.first_seen_at.desc(), DiscoveredItem.id.desc())
    missing = [i for i in db.scalars(_filtered("needs", source, price, dates)) if needs(i, venue_for(db, i.source, i))]
    if show == "needs":  # worked out here, not in SQL: it depends on the venue's usual show times
        ids = {i.id for i in missing}
        matching = [i for i in db.scalars(q) if i.id in ids]
        total, rows = len(matching), matching[offset:offset + limit]
    else:
        total = db.scalar(select(func.count()).select_from(q.subquery()))
        rows = db.scalars(q.limit(limit).offset(offset)).all()
    dups = possible_duplicates(db, rows)
    counted = _narrowed(select(DiscoveredItem.status, func.count()).group_by(DiscoveredItem.status), source, price, dates)
    counts = dict(db.execute(counted).all())
    counts["needs"] = len(missing)
    bad = db.scalars(select(DiscoverySource).where(DiscoverySource.active, ~DiscoverySource.manual,
                                                   DiscoverySource.last_status.in_(["blocked", "error"]))
                     .order_by(func.lower(DiscoverySource.name))).all()
    return DiscoveredPage(items=[_item_out(db, i, dups) for i in rows], total=total, counts=counts,
                          unreadable=[UnreadableSource(id=s.id, name=s.name, status=s.last_status, reason=s.last_error,
                                                       last_checked_at=s.last_checked_at) for s in bad])


@router.get("/items.csv")
def items_csv(db: DB, _: AdminUser, show: Show = "waiting", source: int | None = None, price: Has = "any", dates: Has = "any"):
    """The same list as a spreadsheet."""
    rows = db.scalars(_filtered(show, source, price, dates).order_by(DiscoveredItem.first_seen_at.desc(), DiscoveredItem.id.desc())).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Title", "Venue", "Neighborhood", "Opens", "Closes", "Open run", "Price", "Link", "Source", "Found", "Status"])
    for item in rows:
        o = _item_out(db, item)
        w.writerow([o.title, o.venue_name, o.neighborhood, o.begin_date or "", o.end_date or "", "yes" if o.open_run else "",
                    o.cost_text, o.url, o.source_name, o.first_seen_at.date().isoformat(), o.status])
    name = f"discovered-{show}-{date.today().isoformat()}.csv"
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def still_needs(db: DB, item: DiscoveredItem, body: EventIn) -> str:
    """What the event as it would be approved is missing (CC-88): the item, with what the admin gave."""
    as_approved = SimpleNamespace(kind=item.kind, category=body.category, begin_date=body.begin_date, end_date=body.end_date,
                                  open_run=body.open_run, performances=item.performances, weekly=body.weekly)
    return needs(as_approved, db.get(Venue, body.venue_id) if body.venue_id is not None else None)


@router.post("/items/{item_id}/approve", response_model=DiscoveredItemOut)
def approve(item_id: int, body: EventIn, db: DB, user: AdminUser):
    """Add a found event to the catalog, as edited: live now, at a set time, or waiting for review."""
    item = _item(db, item_id)
    if item.status not in ("new", "rejected", "skipped"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This one is already in the catalog")
    missing = still_needs(db, item, body)
    if missing:  # CC-88: the approve form asks for these
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "Add the date it opens." if missing == "date" else "Choose the days it's on, with a start time for each.")
    event = new_event(db, body, user)
    event.found_by_id = item.source_id
    db.add(event)
    db.flush()
    item.status, item.event_id, item.skip_reason = "approved", event.id, ""
    sync_performances(db, item)  # CC-79: its dates and times, for the planner
    db.commit()
    return _item_out(db, item)


@router.post("/items/{item_id}/reject", response_model=RejectOut)
def reject(item_id: int, body: RejectIn, db: DB, _: AdminUser):
    """Reject a found event. With skip_similar, future ones whose title contains the phrase are
    rejected quietly too (this source or all), and so are any already waiting."""
    item = _item(db, item_id)
    if item.status not in ("new", "skipped"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Only new events can be rejected")
    item.status = "rejected"
    also = 0
    if body.skip_similar:
        phrase = norm_title(body.skip_similar.phrase)
        if not phrase:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Give a phrase with letters or numbers")
        rule = SkipRule(phrase=phrase, source_id=None if body.skip_similar.everywhere else item.source_id, caught=0)
        db.add(rule)
        db.flush()
        waiting = db.scalars(select(DiscoveredItem).where(DiscoveredItem.status == "new", DiscoveredItem.id != item.id)).all()
        for other in waiting:
            if matching_rule(db, other.source_id, other.title) is rule:
                other.status, other.skip_reason = "rejected", rule_reason(rule)
                also += 1
        rule.caught = also + 1
        item.skip_reason = rule_reason(rule)
    db.commit()
    return RejectOut(item=_item_out(db, item), also_rejected=also)


def _settled(item: DiscoveredItem, event: Event | None) -> str:
    return "approved" if event is not None and event.found_by_id == item.source_id else "listed"


@router.post("/items/{item_id}/accept", response_model=DiscoveredItemOut)
def accept(item_id: int, db: DB, _: AdminUser):
    """Apply a proposed update (a price added, dates moved) to the catalog event."""
    item = _item(db, item_id)
    event = db.get(Event, item.event_id) if item.event_id else None
    if item.status != "update" or event is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "There's no update to accept")
    for field, (_, found) in (item.changes or {}).items():
        if field in ("begin_date", "end_date"):
            setattr(event, field, date.fromisoformat(found) if found else None)
        elif field == "min_price":
            event.min_price = None if found is None else Decimal(str(found))
        elif field == "cost_text":
            event.cost_text = found or ""
        elif field == "weekly":
            event.weekly = found or {}
        elif field == "weeks":
            event.weeks = found or {}
    if event.end_date:
        event.open_run = False
    if event.begin_date and event.end_date and event.end_date < event.begin_date:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That would close it before it opens; edit the event instead")
    item.status, item.changes = _settled(item, event), {}
    db.commit()
    return _item_out(db, item)


@router.post("/items/{item_id}/dismiss", response_model=DiscoveredItemOut)
def dismiss(item_id: int, db: DB, _: AdminUser):
    """Keep the catalog as it is. The same change isn't proposed again; a different one would be."""
    item = _item(db, item_id)
    if item.status != "update":
        raise HTTPException(status.HTTP_409_CONFLICT, "There's no update to dismiss")
    item.status = _settled(item, db.get(Event, item.event_id) if item.event_id else None)
    db.commit()
    return _item_out(db, item)


@router.get("/rules", response_model=list[SkipRuleOut])
def rules(db: DB, _: AdminUser):
    """The skip rules, newest first, with how many found events each has caught."""
    return [SkipRuleOut(id=r.id, phrase=r.phrase, source_id=r.source_id, source_name=r.source.name if r.source else None,
                        caught=r.caught, created_at=r.created_at)
            for r in db.scalars(select(SkipRule).order_by(SkipRule.id.desc()))]


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: int, db: DB, _: AdminUser):
    """Stop skipping. Items it already rejected stay rejected (they can still be approved)."""
    rule = db.get(SkipRule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    db.delete(rule)
    db.commit()

