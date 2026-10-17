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


HEADER = ("Event", "Venue", "Begin Date", "End", "Neighborhood", "Entry Cost", "Category")


def sheet_html(rows, header=HEADER, ids=False):
    """HTML shaped like Google's htmlview: numbered rows (a title row, the header row, then the
    data rows). ids=True adds an ID column, filled from each row's "id"."""
    header = header + ("ID",) if ids else header
    trs = []

    def tr(cells):
        n = len(trs) + 1
        trs.append(f'<tr><th id="0R{n - 1}"><div>{n}</div></th>' +
                   "".join(f'<td class="s1">{c}</td>' for c in cells) + "</tr>")
    tr(["Culture Crew list", "", ""])
    tr(header)
    for r in rows:
        title = r["title"]
        if r.get("url"):
            title = f'<a target="_blank" href="https://www.google.com/url?q={r["url"]}&amp;sa=D">{title}</a>'
        tr([title, r["venue"], r.get("begin", ""), r.get("end", ""), r.get("hood", ""),
            r.get("cost", ""), r.get("category", "Theater")] + ([r.get("id", "")] if ids else []))
    return "<table><tbody>" + "".join(trs) + "</tbody></table>"


def row(title, venue="Goodman Theatre", begin=None, end=None, **kw):
    return {"title": title, "venue": venue, "begin": (begin or SOON).strftime("%m/%d/%Y"),
            "end": (end or LATER).strftime("%m/%d/%Y"), "hood": kw.pop("hood", "Loop"), **kw}


def fake(rows, ids=False):
    return lambda: sheet_html(rows, ids=ids)


def sheet_event(db, title, venue="Goodman Theatre", begin=None):
    return db.scalar(select(Event).where(Event.source_id == event_id(title, venue, (begin or SOON).strftime("%m/%d/%Y"))))


# ---- reading the sheet -------------------------------------------------------

def test_parse_finds_the_header_and_unwraps_google_links():
    page = sheet_html([row("Hamlet", url="https://goodman.example/hamlet&amp;x=1", cost="$35&amp;up")])
    parsed = parse_sheet(page)
    assert parsed == [{"event": "Hamlet", "venue": "Goodman Theatre", "begin": SOON.strftime("%m/%d/%Y"),
                       "end": LATER.strftime("%m/%d/%Y"), "hood": "Loop", "cost": "$35&up",
                       "category": "Theater", "id": "", "url": "https://goodman.example/hamlet", "row": 3}]


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


# ---- the sheet's ID column (CC-32) -------------------------------------------

def test_editing_title_venue_or_date_keeps_the_event(client, db, user_token):
    sync_from_sheet(db, fetch=fake([row("Hamlet", id="ev-1"), row("Bug", venue="Steppenwolf", id="ev-2")], ids=True))
    hamlet = db.scalar(select(Event).where(Event.source_id == "ev-1"))
    assert client.put(f"/me/attended/{hamlet.id}", headers=auth(user_token)).status_code == 201

    edited = [row("Hamlet, Prince of Denmark", venue="Court Theatre", hood="Hyde Park",
                  begin=SOON + timedelta(days=1), id="ev-1"),
              row("Bug", venue="Steppenwolf", id="ev-2")]
    run = sync_from_sheet(db, fetch=fake(edited, ids=True))
    assert (run.added, run.updated, run.unchanged, run.withdrawn) == (0, 1, 1, 0)
    db.refresh(hamlet)
    assert hamlet.title == "Hamlet, Prince of Denmark" and hamlet.venue.name == "Court Theatre"
    assert hamlet.begin_date == SOON + timedelta(days=1) and hamlet.status == EventStatus.approved
    assert db.scalar(select(func.count(Attended.event_id))) == 1  # the "I've been" mark is still there
    assert [a["event"]["id"] for a in client.get("/me/attended", headers=auth(user_token)).json()] == [hamlet.id]


def test_adding_the_id_column_with_current_ids_changes_nothing(db):
    rows = [row("Hamlet"), row("Bug", venue="Steppenwolf")]
    sync_from_sheet(db, fetch=fake(rows))
    before = {e.source_id: e.id for e in db.scalars(select(Event))}
    with_ids = [{**r, "id": event_id(r["title"], r["venue"], r["begin"])} for r in rows]
    run = sync_from_sheet(db, fetch=fake(with_ids, ids=True))
    assert (run.added, run.updated, run.unchanged, run.withdrawn) == (0, 0, 2, 0)
    assert {e.source_id: e.id for e in db.scalars(select(Event))} == before
    assert run.message == ""  # every row has an ID now


def test_rows_without_an_id_fall_back_to_the_computed_one(db):
    run = sync_from_sheet(db, fetch=fake([row("Hamlet", id="ev-1"), row("Bug", venue="Steppenwolf")], ids=True))
    assert run.added == 2 and "1 rows have no ID" in run.message
    assert sheet_event(db, "Bug", venue="Steppenwolf") is not None  # computed id, as before
    assert db.scalar(select(Event).where(Event.source_id == "ev-1")).title == "Hamlet"


def test_bad_or_repeated_ids(db):
    too_long = "x" * 41
    run = sync_from_sheet(db, fetch=fake([row("Hamlet", id=too_long), row("Bug", id="same"),
                                          row("Copy of Bug", id="same")], ids=True))
    assert run.added == 2  # the second "same" row is a repeat: the first wins
    assert sheet_event(db, "Hamlet") is not None  # too long to store: computed id instead
    assert db.scalar(select(Event).where(Event.source_id == "same")).title == "Bug"


def test_sheet_ids_script_prints_a_column_that_lines_up(monkeypatch, capsys):
    from scripts import sheet_ids
    rows = [row("Hamlet", id="ev-1"), {**row("Blank"), "title": ""}, row("Bug", venue="Steppenwolf")]
    monkeypatch.setattr(sheet_ids, "fetch_sheet_html", lambda: sheet_html(rows, ids=True))
    sheet_ids.main()
    out = capsys.readouterr().out
    assert "ID column: found. 2 events, 1 without an ID." in out
    assert "Rows missing an ID: 5" in out and "starting at row 3" in out
    column = out.split("ID):\n\n", 1)[1].splitlines()
    assert column == ["ev-1", "", event_id("Bug", "Steppenwolf", SOON.strftime("%m/%d/%Y"))]


def test_an_id_mangled_by_sheets_keeps_its_event(db):
    rows = [row("Hamlet"), row("Bug", venue="Steppenwolf")]
    sync_from_sheet(db, fetch=fake(rows))
    hamlet = sheet_event(db, "Hamlet")
    mangled = [{**rows[0], "id": "5.41E+32"}, {**rows[1], "id": event_id("Bug", "Steppenwolf", rows[1]["begin"])}]
    run = sync_from_sheet(db, fetch=fake(mangled, ids=True))
    assert (run.added, run.withdrawn, run.unchanged) == (0, 0, 2)
    assert "1 rows have an ID that matches no event" in run.message
    assert sheet_event(db, "Hamlet").id == hamlet.id


def test_a_new_id_is_a_new_event_when_another_row_holds_the_old_one(db):
    sync_from_sheet(db, fetch=fake([row("Hamlet")]))  # no ID column yet: computed id
    computed = event_id("Hamlet", "Goodman Theatre", SOON.strftime("%m/%d/%Y"))
    # Row 1 claims the event by its computed id, so row 2's fresh ID is a separate event, not a mangled one
    run = sync_from_sheet(db, fetch=fake([row("Hamlet", id=computed), row("Hamlet", id="ev-2")], ids=True))
    assert (run.added, run.unchanged, run.withdrawn) == (1, 1, 0) and run.message == ""
    assert db.scalar(select(Event).where(Event.source_id == "ev-2")) is not None


def test_sheet_ids_flags_ids_sheets_turned_into_numbers(monkeypatch, capsys):
    from scripts import sheet_ids
    digits_only = next(f"x{i}" for i in range(10000) if event_id(f"x{i}", "V", "1/1/2030").isdigit())
    rows = [{"title": "A", "venue": "V", "begin": "1/1/2030", "id": "5.41E+32"},
            {"title": digits_only, "venue": "V", "begin": "1/1/2030", "id": event_id(digits_only, "V", "1/1/2030")}]
    monkeypatch.setattr(sheet_ids, "fetch_sheet_html", lambda: sheet_html(rows, ids=True))
    sheet_ids.main()
    out = capsys.readouterr().out
    assert "row 3 '5.41E+32'" in out and "row 4" not in out  # an all-digit id that is right isn't flagged
