"""CC-170: "Add to calendar": the event on the day you're going, as a .ics file."""
from datetime import UTC, date, datetime, timedelta

from app.icsfile import _fold, build
from app.models import EventStatus
from app.stats import today_in_chicago

T = today_in_chicago()


def unfold(text: str) -> dict[str, str]:
    lines = text.replace("\r\n ", "").split("\r\n")
    return {k: v for k, _, v in (line.partition(":") for line in lines if line)}


def get(client, event_id, day):
    return client.get(f"/events/{event_id}/calendar.ics", params={"date": day.isoformat()})


def test_a_show_with_a_time_gets_a_timed_entry_at_the_venue(client, db, make_event):
    ev = make_event("Hamlet, Prince of Denmark")
    ev.weekly = {"mon": ["19:30"], "tue": ["19:30"], "wed": ["19:30"], "thu": ["19:30"], "fri": ["14:00", "19:30"],
                 "sat": ["19:30"], "sun": ["19:30"]}
    ev.venue.address, ev.venue.city, ev.venue.zip_code = "170 N Dearborn St", "Chicago", "60601"
    db.commit()
    day = T + timedelta(days=2)
    r = get(client, ev.id, day)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/calendar")
    assert r.headers["content-disposition"] == 'inline; filename="hamlet-prince-of-denmark.ics"'
    assert r.text.startswith("BEGIN:VCALENDAR\r\n") and r.text.endswith("END:VCALENDAR\r\n")
    f = unfold(r.text)
    first = "14:00" if day.weekday() == 4 else "19:30"  # the earliest show that day
    start = datetime.fromisoformat(f"{day}T{first}").replace(tzinfo=__import__("zoneinfo").ZoneInfo("America/Chicago"))
    assert f["DTSTART"] == start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    assert f["DTEND"] == (start + timedelta(hours=2)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    assert f["SUMMARY"] == "Hamlet\\, Prince of Denmark"  # commas escaped
    assert f["LOCATION"] == "Goodman Theatre\\, 170 N Dearborn St\\, Chicago\\, IL 60601"
    assert f["URL"] == f"http://localhost:4173/#e/{ev.id}"
    assert f"/#e/{ev.id}" in f["DESCRIPTION"]
    assert f["UID"] == f"cc-{ev.id}-{day:%Y%m%d}@chiculturecrew.com"  # the same day again replaces, not duplicates


def test_no_known_time_gives_an_all_day_entry(client, make_event):
    ev = make_event("Monet in the garden", category="Art")
    day = T + timedelta(days=1)
    f = unfold(get(client, ev.id, day).text)
    assert f["DTSTART;VALUE=DATE"] == f"{day:%Y%m%d}"
    assert f["DTEND;VALUE=DATE"] == f"{day + timedelta(days=1):%Y%m%d}"
    assert f["LOCATION"] == "Goodman Theatre"  # no address on file: just the venue's name


def test_a_late_show_ends_the_next_day(db, make_event):
    ev = make_event("Late set", begin=None, end=None)  # an open run, so any day is on
    ev.weekly = {k: ["23:00"] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    db.commit()
    f = unfold(build(ev, date(2026, 7, 10), now=datetime(2026, 7, 1, tzinfo=UTC)))
    assert (f["DTSTART"], f["DTEND"]) == ("20260711T040000Z", "20260711T060000Z")  # 11pm-1am CDT
    assert f["DTSTAMP"] == "20260701T000000Z"


def test_long_lines_are_folded_without_splitting_characters():
    line = "SUMMARY:" + "é" * 80
    folded = _fold(line)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == line


def test_hidden_or_missing_events_and_bad_dates(client, make_event):
    pending = make_event("Not yet", status=EventStatus.pending)
    assert get(client, pending.id, T).status_code == 404
    assert get(client, 999999, T).status_code == 404
    live = make_event("Live")
    assert client.get(f"/events/{live.id}/calendar.ics", params={"date": "soon"}).status_code == 422
    assert client.get(f"/events/{live.id}/calendar.ics").status_code == 422
