"""CC-310: a found item gets a candidate picture before anyone approves it, the review screen shows it or says why not,
the admin's own choice is kept, and approving carries the picture over with no wait for the hourly run."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.discovery import fetch as web
from app.discovery import pictures
from app.discovery.readers import FoundEvent
from app.discovery.store import record
from app.models import DiscoveredItem, DiscoverySource, Event, SourceKind

from .conftest import auth
from .test_event_pictures import SITE, _image, _page, _Site, site, stored  # noqa: F401 - fixtures

NOW = datetime(2027, 1, 5, 15, tzinfo=UTC)


@pytest.fixture
def item(db):
    """A new found item on a source whose site is the fake venue."""
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


def _run(db):
    return pictures.add_item_pictures(db, NOW)


def test_a_found_item_gets_the_pages_share_image_before_approval(db, site, stored, item):  # criterion 1
    it = item()
    [(name, status, note)] = _run(db)
    assert (name, status) == ("Venue", "ok") and "venue.example" in note
    db.refresh(it)
    assert it.image_key == f"event-images/found-{it.id}/{it.image_key.rsplit('/', 1)[1]}" and it.image_note == ""
    assert it.image_source_url == SITE + "/seagull"
    assert (stored / it.image_key).is_file()  # copied into our storage, never the venue's address


def test_the_review_list_shows_the_picture_or_the_reason(client, admin_token, db, site, stored, item):  # 1, 3
    ok, none = item(), item("Hamlet", SITE + "/hamlet")
    _Site.ROUTES["/hamlet"] = (200, "text/html", _page())
    _run(db)
    rows = {i["title"]: i for i in client.get("/admin/discovery/items", headers=auth(admin_token)).json()["items"]}
    assert rows["The Seagull"]["image_url"].endswith(".webp") and rows["The Seagull"]["image_note"] == ""
    assert rows["Hamlet"]["image_url"] is None and rows["Hamlet"]["image_note"] == "The page names no picture"


def test_approving_keeps_the_picture_with_no_wait_for_the_hourly_run(client, admin_token, db, site, stored, item):  # 2
    it = item(); _run(db)
    db.refresh(it); key = it.image_key
    body = {"title": "The Seagull", "category": "Theater", "venue_id": None, "venue": {"name": "Venue", "neighborhood": "Loop"},
            "begin_date": "2027-02-01", "end_date": "2027-03-01", "url": it.url, "publish": "now", "weekly": {"thu": ["19:30"]}}
    r = client.post(f"/admin/discovery/items/{it.id}/approve", json=body, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    ev = db.get(Event, r.json()["event"]["id"])
    assert ev.image_key == key and ev.image_source_url == SITE + "/seagull" and ev.image_override is False
    assert pictures.due(db, NOW) == []  # the hourly step has nothing left to do for it


def test_no_usable_picture_says_why_and_still_approves(client, admin_token, db, site, stored, item):  # 3
    _Site.ROUTES["/seagull"] = (200, "text/html", _page('<meta property="og:image" content="/art/tiny.jpg">'))
    _Site.ROUTES["/art/tiny.jpg"] = (200, "image/jpeg", _image(size=(50, 50)))
    it = item(); _run(db); db.refresh(it)
    assert it.image_key is None and "too small" in it.image_note and it.image_tries == pictures.MAX_TRIES
    body = {"title": "The Seagull", "category": "Theater", "venue": {"name": "Venue", "neighborhood": "Loop"},
            "begin_date": "2027-02-01", "end_date": "2027-03-01", "url": it.url, "publish": "now", "weekly": {"thu": ["19:30"]}}
    assert client.post(f"/admin/discovery/items/{it.id}/approve", json=body, headers=auth(admin_token)).status_code == 200


def test_a_temporary_failure_is_tried_again_and_says_so(db, site, stored, item, monkeypatch):
    it = item()
    monkeypatch.setattr(web, "fetch", lambda *a, **k: (_ for _ in ()).throw(web.FetchError("couldn't reach venue.example")))
    [(_, status, _)] = _run(db); db.refresh(it)
    assert status == "error" and it.image_note.startswith("Still trying") and it.image_tries == 1
    assert pictures.due_items(db, NOW) == []  # not again within the hour
    assert pictures.due_items(db, datetime(2027, 1, 5, 17, tzinfo=UTC)) == [it]


def test_the_admins_upload_or_removal_is_kept_and_never_replaced(client, admin_token, db, site, stored, item):  # 4
    up, gone = item(), item("Hamlet", SITE + "/hamlet")
    h = auth(admin_token)
    r = client.put(f"/admin/discovery/items/{up.id}/image", content=_image("PNG"), headers={**h, "Content-Type": "image/png"})
    assert r.status_code == 200 and r.json()["image_override"] and r.json()["image_url"].endswith(".webp")
    r = client.delete(f"/admin/discovery/items/{gone.id}/image", headers=h)
    assert r.status_code == 200 and r.json()["image_url"] is None and r.json()["image_override"]
    db.refresh(up)
    before = (up.image_key, _Site.asked[:])
    assert _run(db) == []  # neither is looked at again
    db.refresh(up); db.refresh(gone)
    assert up.image_key == before[0] and gone.image_key is None and _Site.asked == before[1]
    body = {"title": "Hamlet", "category": "Theater", "venue": {"name": "Venue", "neighborhood": "Loop"},
            "begin_date": "2027-02-01", "end_date": "2027-03-01", "url": gone.url, "publish": "now", "weekly": {"thu": ["19:30"]}}
    ev = db.get(Event, client.post(f"/admin/discovery/items/{gone.id}/approve", json=body, headers=h).json()["event"]["id"])
    assert ev.image_key is None and ev.image_override is True  # so the event's own hourly step skips it too
    assert pictures.due(db, NOW) == []


def test_a_bad_upload_is_refused_and_keeps_what_was_there(client, admin_token, db, site, stored, item):
    it = item(); _run(db); db.refresh(it); key = it.image_key
    h = auth(admin_token)
    assert client.put(f"/admin/discovery/items/{it.id}/image", content=b"<html>", headers={**h, "Content-Type": "image/png"}).status_code == 422
    assert client.put(f"/admin/discovery/items/{it.id}/image", content=b"x", headers={**h, "Content-Type": "image/svg+xml"}).status_code == 415
    db.refresh(it)
    assert it.image_key == key and not it.image_override


def test_a_picture_from_the_tickets_page_is_used_when_the_event_page_has_none(db, site, stored, item):  # 5
    _Site.ROUTES["/seagull"] = (200, "text/html", b'<html><body><a href="/tickets/seagull">Buy tickets</a></body></html>')
    _Site.ROUTES["/tickets/seagull"] = (200, "text/html", _page('<meta property="og:image" content="/art/seagull.jpg">'))
    it = item(); _run(db); db.refresh(it)
    assert it.image_key and it.image_source_url == SITE + "/tickets/seagull"


def test_a_picture_another_event_has_is_not_reused(db, site, stored, item, make_event):  # 6
    it, other = item(), item("Hamlet", SITE + "/hamlet")
    _Site.ROUTES["/hamlet"] = _Site.ROUTES["/seagull"]
    _run(db)  # the first found item takes it; the second sees the same picture
    db.refresh(it); db.refresh(other)
    assert it.image_key and other.image_key is None and "same as another" in other.image_note
    ev = make_event(title="Elsewhere")
    ev.image_key = f"event-images/{ev.id}/{it.image_key.rsplit('/', 1)[1]}"
    third = item("Macbeth", SITE + "/macbeth")
    _Site.ROUTES["/macbeth"] = _Site.ROUTES["/seagull"]
    it.status = "rejected"; db.commit()
    _run(db); db.refresh(third)
    assert third.image_key is None and "same as another" in third.image_note  # an event's picture counts too


def test_refusals_and_ai_opt_outs_are_respected(db, site, stored, item):  # 7
    _Site.ROUTES["/robots.txt"] = (200, "text/plain", b"User-agent: *\nDisallow: /seagull\n")
    it = item(); _run(db); db.refresh(it)
    assert it.image_key is None and it.image_note.startswith("Refused")
    assert not any(a.endswith("/seagull") for a in _Site.asked)  # the page itself was never requested


def test_only_pages_on_the_sources_site_are_read(db, site, stored, item):
    it = item(); it.url = "http://elsewhere.example/seagull"; db.commit()
    [(_, status, note)] = _run(db); db.refresh(it)
    assert status == "none" and "source's site" in it.image_note


def test_an_upload_over_one_megabyte_gets_through_the_body_limit(client, admin_token, db, site, stored, item):  # review: Blocking 1
    import io
    import os
    from PIL import Image
    it = item()
    out = io.BytesIO()
    Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3)).save(out, "PNG")  # noise: about 2.4 MB, over the 1 MB JSON cap
    assert 1_000_000 < len(out.getvalue()) < 3_800_000
    r = client.put(f"/admin/discovery/items/{it.id}/image", content=out.getvalue(), headers={**auth(admin_token), "Content-Type": "image/png"})
    assert r.status_code == 200 and r.json()["image_override"]


def test_an_admins_upload_is_kept_even_when_another_event_has_the_same_file(client, admin_token, db, site, stored, item, make_event):  # Blocking 2
    it = item()
    h = auth(admin_token)
    png = _image("PNG")
    client.put(f"/admin/discovery/items/{it.id}/image", content=png, headers={**h, "Content-Type": "image/png"})
    db.refresh(it)
    ev = make_event(title="Last season")
    ev.image_key = f"event-images/{ev.id}/{it.image_key.rsplit('/', 1)[1]}"; db.commit()  # the same file, on another event
    body = {"title": "The Seagull", "category": "Theater", "venue": {"name": "Venue", "neighborhood": "Loop"},
            "begin_date": "2027-02-01", "end_date": "2027-03-01", "url": it.url, "publish": "now", "weekly": {"thu": ["19:30"]}}
    new = db.get(Event, client.post(f"/admin/discovery/items/{it.id}/approve", json=body, headers=h).json()["event"]["id"])
    assert new.image_key == it.image_key and new.image_override is True


def test_a_picture_found_since_by_sourcing_is_dropped_when_another_event_took_it(client, admin_token, db, site, stored, item, make_event):
    it = item(); _run(db); db.refresh(it)
    ev = make_event(title="Elsewhere")
    ev.image_key = f"event-images/{ev.id}/{it.image_key.rsplit('/', 1)[1]}"; db.commit()
    body = {"title": "The Seagull", "category": "Theater", "venue": {"name": "Venue", "neighborhood": "Loop"},
            "begin_date": "2027-02-01", "end_date": "2027-03-01", "url": it.url, "publish": "now", "weekly": {"thu": ["19:30"]}}
    new = db.get(Event, client.post(f"/admin/discovery/items/{it.id}/approve", json=body, headers=auth(admin_token)).json()["event"]["id"])
    assert new.image_key is None  # the CC-251 rule holds for what sourcing found


def test_an_upload_made_while_the_step_is_fetching_is_not_overwritten(db, Session, site, stored, item, monkeypatch):  # Blocking 3
    it = item()
    real = pictures.picture_from

    def slow(image_url):  # the admin removes the picture in another session while this one is still fetching
        got = real(image_url)
        with Session() as other:
            row = other.get(DiscoveredItem, it.id)
            row.image_key, row.image_override = None, True
            other.commit()
        return got
    monkeypatch.setattr(pictures, "picture_from", slow)
    [(_, status, _)] = _run(db)
    db.expire_all()
    fresh = db.get(DiscoveredItem, it.id)
    assert fresh.image_key is None and fresh.image_override is True and fresh.image_note == ""
    assert not (stored / "event-images").exists() or not list((stored / "event-images").rglob("*.webp"))


def test_the_last_failed_try_says_it_gave_up(db, site, stored, item, monkeypatch):
    it = item()
    monkeypatch.setattr(web, "fetch", lambda *a, **k: (_ for _ in ()).throw(web.FetchError("couldn't reach venue.example")))
    for hours in (0, 2, 4):
        pictures.add_item_pictures(db, datetime(2027, 1, 5, 12 + hours, tzinfo=UTC))
    db.refresh(it)
    assert it.image_tries == pictures.MAX_TRIES and it.image_note.startswith("Gave up after 3 tries")


def test_an_ai_opt_out_does_not_stop_a_picture(db, site, stored, item):  # the agreed rule (CC-246): nothing here goes to Claude
    _Site.ROUTES["/robots.txt"] = (200, "text/plain", b"User-agent: ClaudeBot\nDisallow: /\n")
    it = item(); _run(db); db.refresh(it)
    assert it.image_key


def test_the_hourly_run_looks_for_found_items_pictures(db, monkeypatch):
    from app.discovery import run as runner
    called = []
    monkeypatch.setattr(runner, "add_item_pictures", lambda d: called.append(1) or [])
    monkeypatch.setattr(runner, "add_pictures", lambda d: [])
    runner.run(db, None, datetime(2027, 1, 5, tzinfo=UTC).date())
    assert called
