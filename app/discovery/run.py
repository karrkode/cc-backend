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
- CC-211: from 80%, admins get an email and the sources with the most approved events per dollar go first
- CC-221: every venue with a website gets a source (venuesources.py), a few first reads a day
- CC-88: a found event missing a date or show days has its own page read the day after it's found,
  then weekly for 4 weeks (needs.py); these come first within the 10 event pages per run
- CC-246: newly approved events get their picture from their own page (pictures.py). That costs no tokens, so it runs
  whatever the budget says
- CC-302: a found or live performance whose own page gives no exact dates has a tickets or calendar page it links to
  read too (tickets.py), through the same polite fetch. The extra tokens are logged and counted like any read, and new dates
  for a live event wait in review as an update (they never replace its dates by themselves)
- CC-198: a failed read's tokens count toward the budget, and a source whose reads keep failing waits longer each time
  (1, 2, 4, 8... days, up to 60). Check now still runs it. A page that can't be fetched costs nothing, so that's tomorrow.
"""
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import or_, select

from ..clock import chicago_day, utcnow
from ..models import DiscoveredItem, DiscoveryCheck, DiscoverySource, Event
from . import fetch as web
from .budget import alert_if_short, best_first, budget_state
from .hours import apply_open_hours, apply_show_times, link_venue
from .jsonld import complete, events_in
from .needs import look_again, needs
from .pictures import add_pictures
from .pagetext import page_text, text_hash
from .readers import OWN_PAGE_NOTE, TICKETS_PAGE_NOTE, FoundEvent, ReadFailed, cost_usd, read_aic, read_link_choice, read_page
from .rules import PERFORMING_CATEGORIES, apply_rules, norm_title
from .store import (changes_against, drop_passed_hidden, fields_of, release_delayed, note_addresses, on_source_site, record,
                    venue_for)
from .tickets import MAX_REFUSED, links_of
from .venuesources import add_missing, use_calendar_link

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


def failed_gap(failed_reads: int) -> int:
    """CC-198: the days to wait after this many failed reads in a row."""
    return min(2 ** (failed_reads - 1), MAX_GAP_DAYS)


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
    wait = 1  # after an error: try again tomorrow; nothing was saved as read
    try:
        if source.reader == "aic_api":
            reading = read_aic(today)
            digest = text_hash(json.dumps([e.model_dump() for e in reading.events], sort_keys=True))
            if state.get("api", {}).get("hash") != digest:
                _store(db, source, reading.events, out)
                state["api"] = {"hash": digest}
                out.status = "ok"
        else:
            if source.find_link:
                use_calendar_link(source)
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
                note_addresses(db, source, page.text)  # CC-218: free, whoever reads the events
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
    except ReadFailed as e:
        out.pages_read += 1; out.tokens_in += e.tokens_in; out.tokens_out += e.tokens_out
        source.failed_reads += 1
        wait = failed_gap(source.failed_reads)
        out.status, out.notes = "error", [f"{e} (failed reads in a row: {source.failed_reads}; next try in {wait} days)"]
    except Exception as e:  # a reader failing on one source must not stop the others
        out.status, out.notes = "error", [f"{type(e).__name__}: {e}"[:500]]
    now = utcnow()
    source.last_checked_at, source.last_status = now, out.status
    source.last_error = "; ".join(out.notes) if out.status in ("blocked", "error") else ""
    if out.status == "error":
        source.next_check_on = today + timedelta(days=wait)
        return out
    if out.status != "blocked":
        source.failed_reads = 0
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


def read_event_page(db, client, item: DiscoveredItem, today: date, mark: str) -> tuple[DiscoverySource, Outcome]:
    """Read one found event's own page (its schema.org data first, Claude only when that's incomplete) and store what it says."""
    source, out, festival = db.get(DiscoverySource, item.source_id), Outcome(status="ok"), False
    try:
        if not on_source_site(item.url, source):  # CC-189: a link stored before these were limited to the source's site
            raise web.Blocked("this event's link isn't on the source's site")
        page = web.fetch(item.url)
        note_addresses(db, source, page.text)  # CC-218
        data = events_in(page.text, page.url, source.kind.value)
        same = [e for e in data if norm_title(e.title) == norm_title(item.title)] or (data if len(data) == 1 else [])
        if same and complete(same[0]):  # the page's own event data: no AI needed
            out.notes.append("read from the page's event data")
        else:
            text, _ = page_text(page.text, page.url)
            reading, tin, tout = read_page(client, source.name, source.kind.value, page.url, text, today, OWN_PAGE_NOTE, item.title)
            out.pages_read += 1; out.tokens_in += tin; out.tokens_out += tout
            works = _works_of(reading.events, item.title)
            if len(works) >= MIN_WORKS:  # CC-302: a festival or series: each work is an event of its own
                _store_works(db, source, item, works, out)
                same, festival = [], True
            else:
                same = [e for e in reading.events if norm_title(e.title) == norm_title(item.title)] or reading.events[:1]
        for e in same[:1]:
            e.url = item.url  # the same item, whatever links the page has
            _store(db, source, [e], out)
        live = db.get(Event, item.event_id) if item.event_id else None
        if (item.category in PERFORMING_CATEGORIES and not item.performances and item.kind != "permanent" and not festival
                and not (live is not None and live.show_run_exact)):  # a person's Show Run is theirs: nothing would be proposed
            _read_tickets(db, client, item, source, page, today, out)  # CC-302: still no exact dates
    except web.Blocked as e:
        out.status, out.notes = "blocked", [str(e)]
    except ReadFailed as e:  # each event page is only tried on its own schedule, so there's nothing to back off
        out.pages_read += 1; out.tokens_in += e.tokens_in; out.tokens_out += e.tokens_out  # CC-302: on top of any read before it
        out.status, out.notes = "error", [str(e)]
    except Exception as e:  # noqa: BLE001 - one event page must not stop the rest
        out.status, out.notes = "error", [f"{type(e).__name__}: {e}"[:500]]
    out.notes.insert(0, f"{item.title}: {mark}")
    return source, out



FESTIVAL_NOTE = "A festival or series: its works are separate events"
MIN_WORKS = 2  # CC-302: a page with this many separate works with dates is a festival or series
MAX_WORKS = 12


def _works_of(events: list[FoundEvent], title: str) -> list[FoundEvent]:
    """CC-302: the separate works a festival's own page presents: events other than the page's own, each with dates."""
    return [e for e in events if norm_title(e.title) != norm_title(title) and (e.performances or e.begin_date)][:MAX_WORKS]


def _store_works(db, source: DiscoverySource, item: DiscoveredItem, works: list[FoundEvent], out: Outcome) -> None:
    """CC-302: each work of a festival is a new event, titled with the festival's name first ("New Stages Festival 26/27:
    House Gyalz"). It has no page of its own to link, so it's kept by its title. The festival's own item is left as it was."""
    prefix = norm_title(item.title)
    for e in works:
        if not norm_title(e.title).startswith(prefix):
            e.title = f"{item.title}: {e.title.strip()}"[:300]
        e.url = None
        e.recurring = False
        if not e.category:
            e.category = item.category
        _store(db, source, [e], out)
    item.dates_note = FESTIVAL_NOTE  # CC-302: its own item isn't read again for what it's missing: its dates are its works
    out.notes.append(f"a festival or series: {len(works)} works, each its own event")


def _read_tickets(db, client, item: DiscoveredItem, source: DiscoverySource, page, today: date, out: Outcome) -> None:
    """CC-302: the look for exact dates, with what it cost written in the check's note (its tokens are in the check's totals,
    so they count toward the monthly budget)."""
    before = (out.tokens_in, out.tokens_out)
    try:
        _look_for_dates(db, client, item, source, page, today, out)
    finally:
        tin, tout = out.tokens_in - before[0], out.tokens_out - before[1]
        if tin or tout:
            out.notes.append(f"dates page look: {tin} in, {tout} out, about ${cost_usd(tin, tout):.4f}")


def _look_for_dates(db, client, item: DiscoveredItem, source: DiscoverySource, page, today: date, out: Outcome) -> None:
    """CC-302: the exact dates of a found or live performance, from the tickets or calendar page its own page links to
    (the theater's, or a box office's). Every fetch is web.fetch's: public hosts, robots.txt and AI opt-outs, spacing. A page
    that refuses is noted on the item and never asked again. Dates for a live event go to review as an update."""
    refused = _refused_links(item.dates_note)
    links = [(u, t) for u, t in links_of(page.text, page.url) if u not in refused]
    keep = item.dates_note if refused else ""  # what was refused stays on the item, whatever the later looks find
    if not links:
        item.dates_note = keep or "No tickets or calendar link on its page"
        return
    try:
        pick, tin, tout = read_link_choice(client, page.url, item.title, links)
    except ReadFailed as e:  # the event page's own read still stands
        out.pages_read += 1; out.tokens_in += e.tokens_in; out.tokens_out += e.tokens_out
        item.dates_note = keep or f"Its page's links couldn't be read: {e}"[:MAX_REFUSED]
        return
    out.pages_read += 1; out.tokens_in += tin; out.tokens_out += tout
    if pick is None:
        item.dates_note = keep or "No link on its page looked like one that lists the dates"
        return
    link = links[pick][0]
    try:
        tpage = web.fetch(link)
    except web.Blocked as e:
        line = f"Refused: {link}: {e}"[:MAX_REFUSED]
        item.dates_note = "\n".join([*keep.splitlines()[-2:], line])  # the last three refusals
        out.notes.append(f"dates page not read: {line}")
        return
    except web.FetchError as e:  # costs nothing, and the next scheduled look tries again
        item.dates_note = keep or f"Couldn't read {link}: {e}"[:MAX_REFUSED]
        out.notes.append(f"Couldn't read {link}: {e}"[:MAX_REFUSED])
        return
    data = [e for e in events_in(tpage.text, tpage.url, source.kind.value) if _same_show(e.title, item.title)]
    found = data[0].performances if data else []
    if not found:
        text, _ = page_text(tpage.text, tpage.url)
        try:
            reading, tin, tout = read_page(client, source.name, source.kind.value, tpage.url, text, today, TICKETS_PAGE_NOTE)
        except ReadFailed as e:
            out.pages_read += 1; out.tokens_in += e.tokens_in; out.tokens_out += e.tokens_out
            item.dates_note = keep or f"Its dates page {link} couldn't be read: {e}"[:MAX_REFUSED]
            return
        out.pages_read += 1; out.tokens_in += tin; out.tokens_out += tout
        # a title that matches: a link to another show's page must not give this show its dates
        match = [e for e in reading.events if _same_show(e.title, item.title)]
        found = match[0].performances if match else []
    shows = fields_of(FoundEvent(title=item.title, kind="production", performances=found))["performances"]
    if not shows:
        item.dates_note = "\n".join([f"Read {link}: it lists no dates for this show", *keep.splitlines()])
        return
    item.dates_note = "\n".join([f"Exact dates read from {link}", *keep.splitlines()])
    item.performances = shows
    listed = db.get(Event, item.event_id) if item.event_id else None
    if listed is not None and item.status in ("listed", "update", "approved"):
        diff = changes_against(listed, {"cost_text": "", "min_price": None, "begin_date": None, "end_date": None,
                                        "weekly": {}, "weeks": {}, "performances": shows})
        if diff and diff.get("performances") != (item.changes or {}).get("performances"):  # new, not already proposed or dismissed
            item.changes = {**(item.changes or {}), **diff} if item.status == "update" else diff
            item.status = "update"  # a live event keeps its dates until an admin accepts


def _same_show(a: str, b: str) -> bool:
    """CC-302: a tickets page names a show a little differently ("Mrs. Doubtfire (Chicago)", "Hamlet - Tickets"): the same
    words, or one's whole words inside the other's. A different show's page still doesn't match."""
    na, nb = f" {norm_title(a)} ", f" {norm_title(b)} "
    return len(na) > 5 and len(nb) > 5 and (na in nb or nb in na)


def _refused_links(note: str) -> set[str]:
    """The addresses an item's note says refused (one 'Refused: <address>: <why>' per line)."""
    return {line[len("Refused: "):].split(": ", 1)[0] for line in note.splitlines() if line.startswith("Refused: ")}


def check_missing(db, client, today: date) -> list[tuple[DiscoverySource, Outcome]]:
    """CC-88: read the own page of found events missing a date or show days: the day after they're
    found, then weekly for 4 weeks (needs.look_again)."""
    if client is None:
        return []
    items = db.scalars(select(DiscoveredItem).join(DiscoverySource)
                       .where(DiscoveredItem.status == "new", DiscoveredItem.url != "", DiscoverySource.active,
                              DiscoverySource.reader == "claude").order_by(DiscoveredItem.first_seen_at)).all()
    done = []
    for item in items:
        if len(done) >= DETAIL_LIMIT:
            break
        missing = needs(item, venue_for(db, item.source, item))
        if item.dates_note == FESTIVAL_NOTE or not missing or not look_again(item, _day(item.first_seen_at), today):
            continue
        item.looked_on = [*(item.looked_on or []), today.isoformat()]
        done.append(read_event_page(db, client, item, today, "missing " + ("a date" if missing == "date" else "show days")))
    return done


def check_details(db, client, today: date, limit: int = DETAIL_LIMIT) -> list[tuple[DiscoverySource, Outcome]]:
    """Read upcoming events' own pages about 60 and 45 days before they open."""
    if client is None or limit <= 0:
        return []
    items = db.scalars(select(DiscoveredItem).join(DiscoverySource)
                       .where(DiscoveredItem.status.in_(["new", "listed", "update", "approved"]), DiscoveredItem.url != "",
                              or_(DiscoveredItem.begin_date > today, DiscoveredItem.end_date >= today),  # CC-302: running now too
                       DiscoverySource.active, DiscoverySource.reader == "claude")
                       .order_by(DiscoveredItem.begin_date)).all()
    done = []
    for item in items:
        if len(done) >= limit:
            break
        days = (item.begin_date - today).days
        mark = next((d for d in DETAIL_DAYS if days <= d and d not in (item.detail_checks or [])), None)
        if mark is None:
            continue
        found_today = item.first_seen_at and _day(item.first_seen_at) >= today  # its list page was just read
        if found_today:
            continue  # the next run looks again
        # too soon, or nothing left to learn. CC-302: a performance still without exact dates is read however soon it opens
        # (or even while it runs): its own page or tickets page is where the dates are, and they matter most now
        wants_dates = item.category in PERFORMING_CATEGORIES and not item.performances and item.kind != "permanent"
        if (days < DETAIL_SOON_DAYS and not wants_dates) or (item.cost_text and item.performances):
            item.detail_checks = sorted({*(item.detail_checks or []), *DETAIL_DAYS})
            continue
        source, out = read_event_page(db, client, item, today, f"{mark} days before opening")
        item.detail_checks = sorted({*(item.detail_checks or []), *[d for d in DETAIL_DAYS if days <= d]})
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
    release_delayed(db, utcnow())  # CC-257
    drop_passed_hidden(db, today)  # CC-207: costs nothing, so it runs whatever the budget says
    if only is None and add_missing(db, today):  # CC-221: adding one costs nothing either: only its reads do
        db.commit()
    sources = due(db, today, only, force)
    if budget_state(db)[0] != "ok":  # CC-211: what's left goes to the best sources
        sources = best_first(db, sources)
    for source in sources:
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
        else:
            missing = check_missing(db, client, today)
            for source, out in missing + check_details(db, client, today, DETAIL_LIMIT - len(missing)):
                log(db, source, out, what="detail")
                db.commit()
                results.append((source.name + " (event page)", out))
        for name, status, note in add_pictures(db):  # CC-246
            results.append((name + " (picture)", Outcome(status=status, notes=[note])))
    alert_if_short(db)
    return results
