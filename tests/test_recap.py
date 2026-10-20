"""CC-77: the weekly discovery recap email."""
from datetime import UTC, datetime, timedelta

import pytest

from app import mail
from app.discovery.readers import FoundEvent
from app.discovery.recap import build_recap
from app.discovery.store import record
from app.models import DiscoveryCheck, DiscoveredItem, SkipRule
from scripts import recap as recap_script

from .test_discovery import make_source


def found(title, **kw):
    return FoundEvent(title=title, kind=kw.pop("kind", "production"), category="Theater", **kw)


def test_a_busy_week(db, make_event):
    s = make_source(db)
    record(db, s, found("Hamlet"), "")
    record(db, s, found("Macbeth"), "")
    record(db, s, found("Acting 101", kind="class"), "a class or workshop")
    db.add(SkipRule(phrase="student jam", source_id=s.id, caught=0)); db.flush()
    record(db, s, found("Student Jam"), "")
    listed = make_event("Othello", venue_name="Goodman Theatre", neighborhood="Loop")
    listed.cost_text = ""; db.flush()
    record(db, s, found("Othello", cost_text="$30", min_price=30), "")
    old = DiscoveredItem(source_id=s.id, key="old", status="new", title="Last Month", first_seen_at=datetime.now(UTC) - timedelta(days=20))
    db.add(old)
    s.last_status, s.last_error, s.last_checked_at = "blocked", "the site refused the request (HTTP 403)", datetime.now(UTC)
    db.add(DiscoveryCheck(source_id=s.id, status="ok", pages_read=3, input_tokens=100_000, output_tokens=20_000))
    db.commit()

    email = build_recap(db)
    assert email.to == "info@chiculturecrew.com"
    assert email.subject == "Event discovery: 2 new, 1 update, 1 to check by hand"
    t = email.text
    assert "Waiting for review: 4 (3 new events, 1 proposed update)" in t  # Hamlet, Macbeth, Last Month + Othello
    assert "/#found" in t and "/#sources" in t
    assert "NEW EVENTS FOUND THIS WEEK: 2\n  Goodman  2" in t  # not last month's, not the rule's, not the class
    assert "Othello (Goodman): price none -> $30" in t
    assert "By your skip rules:\n    “student jam”  1" in t and "A class or workshop  1" in t
    assert "Goodman: the site refused the request (HTTP 403) (last tried" in t and "https://goodman.example/season" in t
    assert "Checks this week: 1, 3 pages read, about $0.02" in t


def test_a_quiet_week_still_sends(db):
    make_source(db)
    email = build_recap(db)
    assert email.subject == "Event discovery: nothing new this week"
    assert "Nothing new this week" in email.text and "Waiting for review: 0" in email.text
    assert "None. Every source could be read." in email.text


def test_the_script_sends_and_fails_loudly(db, Session, monkeypatch, capsys):
    monkeypatch.setattr(recap_script, "SessionLocal", Session)
    mail.OUTBOX.clear()
    monkeypatch.setattr(recap_script, "build_recap", lambda _db: mail.Email(to="info@chiculturecrew.com", subject="Recap", text="x"))
    monkeypatch.setattr("sys.argv", ["recap"])
    recap_script.main()
    assert [e.subject for e in mail.OUTBOX] == ["Recap"]

    def broken(email, strict=False):
        assert strict
        raise RuntimeError("SES said no")
    monkeypatch.setattr(recap_script, "send", broken)
    with pytest.raises(SystemExit) as stop:
        recap_script.main()
    assert stop.value.code == 1 and "SES said no" in capsys.readouterr().err
