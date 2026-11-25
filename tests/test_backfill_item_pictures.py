"""CC-318: the one-off catch-up that gives review-list items a proposed picture. Fixtures only (the fake venue site of
test_event_pictures); no live pages."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.discovery import pictures
from app.discovery.readers import FoundEvent
from app.discovery.store import record
from app.models import DiscoveredItem, DiscoverySource, Event, SourceKind
from scripts import backfill_item_pictures as job

from .test_event_pictures import SITE, _image, _page, _Site, site, stored  # noqa: F401 - fixtures

NOW = datetime(2027, 1, 5, 15, tzinfo=UTC)


@pytest.fixture
def item(db):
    source = DiscoverySource(name="Venue", kind=SourceKind.theater, pages=[SITE + "/season"], reader="claude",
                             venue_name="Venue", neighborhood="Loop", every_days=30, page_state={}, venue_hours={},
                             unchanged_streak=0, last_status="", last_error="")
    db.add(source); db.commit()

    def _make(title="The Seagull", url=SITE + "/seagull"):
        record(db, source, FoundEvent(title=title, url=url, kind="production", category="Theater",
                                      begin_date="2027-02-01", end_date="2027-03-01"), "")
        db.commit()
        return db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == title))
    return _make


def test_a_dry_run_reports_the_counts_and_fetches_nothing(db, site, stored, item):  # criterion 4
    item(); item("Hamlet", SITE + "/hamlet")
    asked = _Site.asked[:]
    counts = job.report(db)
    assert counts["found"] == 2 and counts["todo"] == 2 and counts["has_picture"] == 0
    assert "Dry run" in job.summary(counts, None, True)
    assert _Site.asked == asked and all(i.image_key is None and i.image_tries == 0 for i in db.scalars(select(DiscoveredItem)))


def test_the_real_run_proposes_a_picture_or_records_why_not(db, site, stored, item):  # 1
    ok, none = item(), item("Hamlet", SITE + "/hamlet")
    _Site.ROUTES["/hamlet"] = (200, "text/html", _page())
    results = pictures.backfill_items(db, now=NOW)
    assert sorted(s for _, s, _ in results) == ["none", "ok"]
    db.refresh(ok); db.refresh(none)
    assert ok.image_key and ok.image_source_url == SITE + "/seagull" and ok.image_note == ""
    assert none.image_key is None and none.image_note == "The page names no picture"
    after = job.report(db)
    assert after["has_picture"] == 1 and after["reason"] == 1 and after["todo"] == 0


def test_running_it_again_changes_nothing_and_fetches_nothing(db, site, stored, item):  # 2
    a = item(); item("Hamlet", SITE + "/hamlet")
    _Site.ROUTES["/hamlet"] = (200, "text/html", _page())
    pictures.backfill_items(db, now=NOW)
    db.refresh(a)
    before = {i.id: (i.image_key, i.image_note, i.image_tries, i.image_checked_at) for i in db.scalars(select(DiscoveredItem))}
    asked = _Site.asked[:]
    assert pictures.backfill_items(db, now=NOW) == []
    db.expire_all()
    assert {i.id: (i.image_key, i.image_note, i.image_tries, i.image_checked_at) for i in db.scalars(select(DiscoveredItem))} == before
    assert _Site.asked == asked


def test_an_existing_picture_and_an_admins_choice_are_never_replaced(db, site, stored, item):  # 2
    has, chosen = item(), item("Hamlet", SITE + "/hamlet")
    has.image_key = "event-images/found-1/mine.webp"; has.image_source_url = "https://elsewhere.example/p"
    chosen.image_override = True
    db.commit()
    asked = _Site.asked[:]
    assert pictures.backfill_items(db, retry_missing=True, now=NOW) == []
    db.refresh(has); db.refresh(chosen)
    assert has.image_key == "event-images/found-1/mine.webp" and chosen.image_key is None and chosen.image_override
    assert _Site.asked == asked


def test_retry_missing_looks_again_where_the_last_look_gave_a_reason(db, site, stored, item):
    it = item()
    _Site.ROUTES["/seagull"] = (200, "text/html", _page())
    pictures.backfill_items(db, now=NOW); db.refresh(it)
    assert it.image_key is None and it.image_note
    assert pictures.backfill_items(db, now=NOW) == []  # skipped by default
    _Site.ROUTES["/seagull"] = (200, "text/html", _page('<meta property="og:image" content="/art/seagull.jpg">'))
    [(_, status, _)] = pictures.backfill_items(db, retry_missing=True, now=NOW); db.refresh(it)
    assert status == "ok" and it.image_key and it.image_note == ""


def test_the_limit_caps_how_many_are_looked_at(db, site, stored, item):
    item(); item("Hamlet", SITE + "/hamlet")
    assert len(pictures.backfill_items(db, limit=1, now=NOW)) == 1
    assert job.report(db)["todo"] == 1


def test_live_events_and_other_statuses_are_left_alone(db, site, stored, item, make_event):  # scope
    it = item(); rejected = item("Hamlet", SITE + "/hamlet"); rejected.status = "rejected"; db.commit()
    ev = make_event(title="Live one"); ev.url = SITE + "/seagull"; ev.found_by_id = it.source_id; db.commit()
    counts = job.report(db)
    assert counts["found"] == 1 and counts["live_without_picture"] >= 1
    pictures.backfill_items(db, now=NOW)
    db.refresh(ev); db.refresh(rejected)
    assert ev.image_key is None and rejected.image_key is None and rejected.image_tries == 0


def test_an_item_with_no_page_address_is_skipped(db, site, stored, item):
    it = item(); it.url = ""; db.commit()
    assert job.report(db)["no_page"] == 1 and pictures.backfill_items(db, now=NOW) == []


def test_the_summary_names_the_outcome_counts(db, site, stored, item):  # 6
    item(); item("Hamlet", SITE + "/hamlet")
    _Site.ROUTES["/hamlet"] = (200, "text/html", _page())
    counts = job.report(db)
    text = job.summary(counts, pictures.backfill_items(db, now=NOW), False)
    assert "1 pictures proposed, 1 with no picture" in text and "0 errors" in text
