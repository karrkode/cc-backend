"""The Google Sheet reader and the nightly sync, run against a fake copy of the sheet's HTML."""
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Attended, Event, EventStatus, SheetSync, User
from app.sheet import SheetUnavailable, event_id, parse_sheet, read_sheet
from app.sync import sync_from_sheet

from .conftest import auth

SOON = date.today() + timedelta(days=10)
LATER = date.today() + timedelta(days=60)


def sheet_html(rows, header=("Event", "Venue", "Begin Date", "End", "Neighborhood", "Entry Cost", "Category")):
    """HTML shaped like Google's htmlview: a title row, the header row, then the data rows."""
    def tr(cells):
        return "<tr>" + "".join(f'<td class="s1">{c}</td>' for c in cells) + "</tr>"
    body = [tr(["Culture Crew list", "", ""]), tr(header)]
    for r in rows:
        title = r["title"]
        if r.get("url"):
            title = f'<a target="_blank" href="https://www.google.com/url?q={r["url"]}&amp;sa=D">{title}</a>'
        body.append(tr([title, r["venue"], r.get("begin", ""), r.get("end", ""), r.get("hood", ""),
                        r.get("cost", ""), r.get("category", "Theater")]))
    return "<table><tbody>" + "".join(body) + "</tbody></table>"


def row(title, venue="Goodman Theatre", begin=None, end=None, **kw):
    return {"title": title, "venue": venue, "begin": (begin or SOON).strftime("%m/%d/%Y"),
            "end": (end or LATER).strftime("%m/%d/%Y"), "hood": kw.pop("hood", "Loop"), **kw}


def fake(rows):
    return lambda: sheet_html(rows)


def sheet_event(db, title, venue="Goodman Theatre", begin=None):
    return db.scalar(select(Event).where(Event.source_id == event_id(title, venue, (begin or SOON).strftime("%m/%d/%Y"))))


# ---- reading the sheet -------------------------------------------------------

def test_parse_finds_the_header_and_unwraps_google_links():
    page = sheet_html([row("Hamlet", url="https://goodman.example/hamlet&amp;x=1", cost="$35&amp;up")])
    parsed = parse_sheet(page)
    assert parsed == [{"event": "Hamlet", "venue": "Goodman Theatre", "begin": SOON.strftime("%m/%d/%Y"),
                       "end": LATER.strftime("%m/%d/%Y"), "hood": "Loop", "cost": "$35&up",
                       "category": "Theater", "url": "https://goodman.example/hamlet"}]


def test_columns_can_be_reordered():
    page = sheet_html([], header=("Venue", "Event", "Category"))
    page = page.replace("</tbody>", '<tr><td>Steppenwolf</td><td>Bug</td><td>Theater</td></tr></tbody>')
    assert parse_sheet(page)[0]["event"] == "Bug" and parse_sheet(page)[0]["venue"] == "Steppenwolf"


def test_normalizing_rows():
    events = read_sheet(fake([
        row("Hamlet", cost="Tickets $35–$90"),
        row("Hamlet", cost="Tickets $35–$90"),                       # entered twice
        row("Matt Rife: Stay Golden World Tour", category="Theater"),  # really comedy
        row("Mystery", venue="Goodman Theatre", category=""),        # blank: take the venue's usual
        row("Gallery", venue="Small Gallery", hood="Evanston, IL", cost="Free", category="Visual Art"),
        {**row("TBD show"), "begin": "TBD", "end": "Open run"},
    ]))
    by_title = {e["title"]: e for e in events}
    assert len(events) == 5
    assert by_title["Hamlet"]["min"] == 35.0 and by_title["Hamlet"]["type"] == "Theater"
    assert by_title["Matt Rife: Stay Golden World Tour"]["type"] == "Comedy"
    assert by_title["Mystery"]["type"] == "Theater"
    assert by_title["Gallery"]["area"] == "Suburbs" and by_title["Gallery"]["min"] == 0
    assert by_title["TBD show"]["begin"] is None and by_title["TBD show"]["openRun"] is True


def test_unreadable_sheets_raise():
    with pytest.raises(SheetUnavailable):
        parse_sheet("<html>Sign in to continue</html>")
    with pytest.raises(SheetUnavailable):
        parse_sheet(sheet_html([]))


# ---- syncing -----------------------------------------------------------------

def test_first_sync_adds_everything(db):
    run = sync_from_sheet(db, fetch=fake([row("Hamlet"), row("Bug", venue="Steppenwolf", hood="Lincoln Park")]))
    assert (run.result, run.rows_read, run.added) == ("ok", 2, 2)
    assert db.scalar(select(func.count(Event.id)).where(Event.status == EventStatus.approved)) == 2


def test_changes_on_the_sheet_update_events(db):
    sync_from_sheet(db, fetch=fake([row("Hamlet", cost="$35"), row("Bug")]))
    run = sync_from_sheet(db, fetch=fake([row("Hamlet", cost="$40", url="https://new.example/hamlet"), row("Bug")]))
    assert (run.updated, run.unchanged, run.added) == (1, 1, 0)
    hamlet = sheet_event(db, "Hamlet")
    db.refresh(hamlet)
    assert float(hamlet.min_price) == 40 and hamlet.url == "https://new.example/hamlet"


def test_removed_rows_are_withdrawn_not_deleted_and_can_come_back(client, db, user_token):
    sync_from_sheet(db, fetch=fake([row("Hamlet"), row("Bug"), row("Cats")]))
    hamlet = sheet_event(db, "Hamlet")
    client.put(f"/me/attended/{hamlet.id}", headers=auth(user_token))

    run = sync_from_sheet(db, fetch=fake([row("Bug"), row("Cats")]))
    assert run.withdrawn == 1
    db.refresh(hamlet)
    assert hamlet.status == EventStatus.withdrawn
    assert client.get(f"/events/{hamlet.id}").status_code == 404           # hidden from the site
    assert db.scalar(select(func.count()).select_from(Attended)) == 1      # but the mark is kept

    run = sync_from_sheet(db, fetch=fake([row("Hamlet"), row("Bug"), row("Cats")]))
    assert run.restored == 1
    db.refresh(hamlet)
    assert hamlet.status == EventStatus.approved


def test_admin_decisions_and_site_events_are_left_alone(db, make_event):
    sync_from_sheet(db, fetch=fake([row("Hamlet"), row("Bug")]))
    bug = sheet_event(db, "Bug")
    bug.status = EventStatus.rejected
    db.commit()
    site_event = make_event("Added on the site")

    sync_from_sheet(db, fetch=fake([row("Hamlet"), row("Bug", cost="$10")]))
    db.refresh(bug)
    db.refresh(site_event)
    assert bug.status == EventStatus.rejected and bug.cost_text == "$10"  # details follow the sheet
    assert site_event.status == EventStatus.approved                      # not on the sheet, never withdrawn


def test_a_sheet_that_suddenly_shrinks_is_refused(db):
    sync_from_sheet(db, fetch=fake([row(f"Show {i}") for i in range(10)]))
    run = sync_from_sheet(db, fetch=fake([row("Show 0"), row("Show 1")]))
    assert run.result == "failed" and "Not withdrawing" in run.message
    assert db.scalar(select(func.count(Event.id)).where(Event.status == EventStatus.approved)) == 10


def test_unreachable_sheet_changes_nothing_and_is_logged(db):
    sync_from_sheet(db, fetch=fake([row("Hamlet")]))

    def broken():
        raise SheetUnavailable("The sheet could not be reached (TimeoutError).")
    run = sync_from_sheet(db, fetch=broken)
    assert run.result == "failed" and "TimeoutError" in run.message
    assert sheet_event(db, "Hamlet").status == EventStatus.approved


def test_dry_run_changes_nothing(db):
    sync_from_sheet(db, fetch=fake([row("Hamlet")]))
    run = sync_from_sheet(db, dry_run=True, fetch=fake([row("Bug", venue="New Place")]))
    assert (run.result, run.added, run.withdrawn) == ("dry_run", 1, 1)
    assert db.scalar(select(func.count(Event.id))) == 1
    assert sheet_event(db, "Hamlet").status == EventStatus.approved


def test_sync_log_is_admin_only(client, db, user_token, admin_token):
    sync_from_sheet(db, fetch=fake([row("Hamlet")]))
    sync_from_sheet(db, fetch=lambda: "<html></html>")
    assert client.get("/admin/syncs", headers=auth(user_token)).status_code == 403
    runs = client.get("/admin/syncs", headers=auth(admin_token)).json()
    assert [r["result"] for r in runs] == ["failed", "ok"]  # newest first
    assert db.scalar(select(func.count(SheetSync.id))) == 2
    assert db.scalar(select(func.count(User.id))) == 2
