"""Which sources are due, checking them, and the pre-opening checks.

Reading as little as possible (agreed with the product owner):
- each source has a usual gap (every_days); after 3 checks in a row with nothing changed the gap
  stretches by half, up to 60 days, and goes back to normal as soon as something changes
- a page is only sent to the model when it has changed (304 Not Modified, or the same text as last time)
- for each upcoming event, just its own page is read about 60 and 45 days before it opens, when
  theaters usually fill in prices and dates (CC-80: not for events found today, opening within a week,
  or whose price and showtimes are already known)
- CC-80: a page's own structured event data (schema.org) is used instead of Claude when it's complete
- CC-80: a monthly budget (budget.py): from 80% event pages wait; at 100% only Check now runs
"""
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from ..models import DiscoveredItem, DiscoveryCheck, DiscoverySource
from ..stats import chicago_day
from . import fetch as web
from .budget import budget_state
from .hours import apply_open_hours, apply_show_times, link_venue
from .jsonld import complete, events_in
from .pagetext import page_text, text_hash
from .readers import read_aic, read_page
from .rules import apply_rules, norm_title
from .store import record

STRETCH_AFTER = 3
MAX_GAP_DAYS = 60
DETAIL_DAYS = (60, 45)
DETAIL_LIMIT = 10  # event pages per run, at most
DETAIL_SOON_DAYS = 7  # an event opening within a week isn't read again: there's no time for news to matter
MIN_LISTED = 3  # a list page's structured data is trusted when it has at least this many complete events


@dataclass
class Outcome:
    status: str = "unchanged"  # ok | unchanged | blocked | error | waiting (the budget's used up)
    new: int = 0
    updates: int = 0
    skipped: int = 0
    pages_read: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    notes: list[str] = field(default_factory=list)


def next_gap(source: DiscoverySource) -> int:
    gap = source.every_days * (1.5 if source.unchanged_streak >= STRETCH_AFTER else 1)
    return int(min(max(gap, 1), max(MAX_GAP_DAYS, source.every_days)))


def due(db, today: date, only: int | None = None, force: bool = False) -> list[DiscoverySource]:
    q = select(DiscoverySource).order_by(DiscoverySource.id)
    if only is not None:
        q = q.where(DiscoverySource.id == only)
    sources = db.scalars(q).all()
    # a source checked by hand only (CC-81) runs when an admin asks (Check now, or --now), never on its own
    return [s for s in sources if force or s.check_requested or
            (s.active and not s.manual and (s.next_check_on is None or s.next_check_on <= today))]


def _store(db, source: DiscoverySource, events, out: Outcome) -> None:
    for found, skip in apply_rules(events, source.kind.value):
        what = record(db, source, found, skip)
        if what == "new":
            out.new += 1
        elif what == "update":
            out.updates += 1
        elif what == "skipped":
            out.skipped += 1


def check_source(db, source: DiscoverySource, client, today: date) -> Outcome:
    out = Outcome()
    state = dict(source.page_state or {})
    try:
        if source.reader == "aic_api":
            reading = read_aic(today)
            digest = text_hash(json.dumps([e.model_dump() for e in reading.events], sort_keys=True))
            if state.get("api", {}).get("hash") != digest:
                _store(db, source, reading.events, out)
                state["api"] = {"hash": digest}
                out.status = "ok"
        else:
            for url in source.pages:
                prev = state.get(url, {})
                page = web.fetch(url, prev.get("etag", ""), prev.get("last_modified", ""))
                if page.status == 304:
                    continue
                text, cut = page_text(page.text, page.url)
                digest = text_hash(text)
                if digest == prev.get("hash"):
                    state[url] = {**prev, "etag": page.etag, "last_modified": page.last_modified}
                    continue
                listed = events_in(page.text, page.url, source.kind.value)
                if sum(complete(e) for e in listed) >= MIN_LISTED:  # the page's own event data: no AI needed
                    _store(db, source, listed, out)
                    out.notes.append(f"{url}: read from the page's event data")
                    state[url] = {"etag": page.etag, "last_modified": page.last_modified, "hash": digest}
                    out.status = "ok"
                    continue
                if client is None:
                    raise web.FetchError("no Anthropic API key is set on the server, so changed pages can't be read")
                reading, tin, tout = read_page(client, source.name, source.kind.value, page.url, text, today)
                out.pages_read += 1; out.tokens_in += tin; out.tokens_out += tout
                if cut:
                    out.notes.append(f"{url}: page was long and cut short")
                _store(db, source, reading.events, out)
                if reading.venue_hours:
                    source.venue_hours = {h.weekday: [h.open, h.close] for h in reading.venue_hours}
                state[url] = {"etag": page.etag, "last_modified": page.last_modified, "hash": digest}
                out.status = "ok"
    except web.Blocked as e:
        out.status, out.notes = "blocked", [str(e)]
    except web.FetchError as e:
        out.status, out.notes = "error", [str(e)]
    except Exception as e:  # a reader failing on one source must not stop the others
        out.status, out.notes = "error", [f"{type(e).__name__}: {e}"[:500]]
    now = datetime.now(UTC)
    source.last_checked_at, source.last_status = now, out.status
    source.last_error = "; ".join(out.notes) if out.status in ("blocked", "error") else ""
    if out.status == "error":
        source.next_check_on = today + timedelta(days=1)  # try again tomorrow; nothing was saved as read
        return out
    source.page_state = state
    if out.status == "ok":
        source.unchanged_streak, source.last_changed_at = 0, now
        link_venue(db, source)
        if apply_open_hours(source):  # CC-78: the venue's hours, unless an admin set them
            out.notes.append("venue opening hours filled in")
        if apply_show_times(db, source, today):
            out.notes.append("usual show times filled in")
    else:
        source.unchanged_streak += 1
    source.next_check_on = today + timedelta(days=next_gap(source))
    return out


def check_details(db, client, today: date) -> list[tuple[DiscoverySource, Outcome]]:
    """Read upcoming events' own pages about 60 and 45 days before they open."""
    if client is None:
        return []
    items = db.scalars(select(DiscoveredItem).join(DiscoverySource)
                       .where(DiscoveredItem.status.in_(["new", "listed", "update", "approved"]), DiscoveredItem.url != "",
                              DiscoveredItem.begin_date > today, DiscoverySource.active, DiscoverySource.reader == "claude")
                       .order_by(DiscoveredItem.begin_date)).all()
    done = []
    for item in items:
        if len(done) >= DETAIL_LIMIT:
            break
        days = (item.begin_date - today).days
        mark = next((d for d in DETAIL_DAYS if days <= d and d not in (item.detail_checks or [])), None)
        if mark is None:
            continue
        found_today = item.first_seen_at and _day(item.first_seen_at) >= today  # its list page was just read
        if found_today:
            continue  # the next run looks again
        if days < DETAIL_SOON_DAYS or (item.cost_text and item.performances):  # too soon, or nothing left to learn
            item.detail_checks = sorted({*(item.detail_checks or []), *DETAIL_DAYS})
            continue
        source, out = db.get(DiscoverySource, item.source_id), Outcome(status="ok")
        try:
            page = web.fetch(item.url)
            data = events_in(page.text, page.url, source.kind.value)
            same = [e for e in data if norm_title(e.title) == norm_title(item.title)] or (data if len(data) == 1 else [])
            if same and complete(same[0]):  # the page's own event data: no AI needed
                out.notes.append("read from the page's event data")
            else:
                text, _ = page_text(page.text, page.url)
                reading, tin, tout = read_page(client, source.name, source.kind.value, page.url, text, today)
                out.pages_read, out.tokens_in, out.tokens_out = 1, tin, tout
                same = [e for e in reading.events if norm_title(e.title) == norm_title(item.title)] or reading.events[:1]
            for e in same[:1]:
                e.url = item.url  # the same item, whatever links the page has
                _store(db, source, [e], out)
        except web.Blocked as e:
            out.status, out.notes = "blocked", [str(e)]
        except Exception as e:  # noqa: BLE001 - one event page must not stop the rest
            out.status, out.notes = "error", [f"{type(e).__name__}: {e}"[:500]]
        item.detail_checks = sorted({*(item.detail_checks or []), *[d for d in DETAIL_DAYS if days <= d]})
        out.notes.insert(0, f"{item.title}: {mark} days before opening")
        done.append((source, out))
    return done


def log(db, source: DiscoverySource, out: Outcome, what: str = "source") -> None:
    db.add(DiscoveryCheck(source_id=source.id, what=what, status=out.status, new_items=out.new, updates=out.updates,
                          skipped=out.skipped, pages_read=out.pages_read, input_tokens=out.tokens_in,
                          output_tokens=out.tokens_out, note="; ".join(out.notes)[:2000]))


def _day(ts: datetime) -> date:
    return chicago_day(ts if ts.tzinfo else ts.replace(tzinfo=UTC))  # SQLite hands back naive UTC


def run(db, client, today: date, only: int | None = None, force: bool = False) -> list[tuple[str, Outcome]]:
    """Check what's due, within the monthly budget (CC-80). force (the command line's --now) and
    Check now (source.check_requested) still run when the budget is used up."""
    results = []
    for source in due(db, today, only, force):
        state, spent, budget = budget_state(db)
        if state == "paused" and not (force or source.check_requested):
            results.append((source.name, Outcome(status="waiting", notes=[
                f"the monthly budget is used up (${spent:.2f} of ${budget:.2f}); it waits for next month, or Check now"])))
            continue
        out = check_source(db, source, client, today)
        source.check_requested = False
        log(db, source, out)
        db.commit()
        results.append((source.name, out))
    if only is None:
        state, spent, budget = budget_state(db)
        if state != "ok":
            results.append(("Event pages", Outcome(status="waiting", notes=[
                f"${spent:.2f} of the ${budget:.2f} monthly budget is spent, so event pages wait for next month"])))
            return results
        for source, out in check_details(db, client, today):
            log(db, source, out, what="detail")
            db.commit()
            results.append((source.name + " (event page)", out))
    return results
