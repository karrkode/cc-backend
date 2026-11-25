"""CC-319: the hourly run always finishes. An item with an end date and no start date no longer crashes it, each step runs on
its own, a picture look is counted before the fetch, and pictures are shrunk while decoded (or refused when too big to be)."""
import io
import sys
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from app import event_images
from app.discovery import pictures
from app.discovery import run as run_mod
from app.discovery.readers import FoundEvent
from app.discovery.run import Outcome, check_details, run
from app.discovery.store import record
from app.models import DiscoveredItem, DiscoverySource, Event
from app.stats import today_in_chicago

from .test_discovery import make_source

TODAY = today_in_chicago()
NOW = datetime(2027, 1, 5, 15, tzinfo=UTC)


# ---- criterion 1: end date, no start date -------------------------------------------------------------------

def test_an_item_with_an_end_date_and_no_start_date_is_read_as_already_open(db, monkeypatch):
    s = make_source(db)
    record(db, s, FoundEvent(title="Hamlet", url="https://goodman.example/shows/hamlet", kind="production", category="Theater",
                             begin_date=(TODAY + timedelta(days=40)).isoformat(),
                             end_date=(TODAY + timedelta(days=90)).isoformat()), "")
    db.commit()
    item = db.scalar(select(DiscoveredItem))
    item.begin_date = None  # CC-302 lets it in: it has an end date still ahead
    item.end_date = TODAY + timedelta(days=30)
    item.first_seen_at = item.first_seen_at - timedelta(days=3)
    db.commit()
    read = []
    monkeypatch.setattr(run_mod, "read_event_page", lambda db_, c, it, today, mark: (read.append(mark) or (s, Outcome())))
    done = check_details(db, object(), TODAY)  # used to raise: None - date
    assert len(done) == 1 and len(read) == 1


# ---- criteria 2 and 3: every step on its own ----------------------------------------------------------------

STEPS = ["_sources", "_event_pages", "_found_pictures", "_live_pictures", "_photo_sweep", "_budget_alert"]


@pytest.mark.parametrize("broken", STEPS)
def test_a_failing_step_is_rolled_back_and_the_others_still_run(db, monkeypatch, broken):
    ran = []
    for name in STEPS:
        def fake(*a, _n=name, **k):
            ran.append(_n)
            if _n == broken:
                db.add(DiscoverySource(name="half-done", kind="theater", pages=[], reader="claude", page_state={}, venue_hours={}))
                raise RuntimeError("boom")
            a[-1].append((_n, Outcome(status="ok")))  # the step's rows go on the results list, its last argument
        monkeypatch.setattr(run_mod, name, fake)
    said = []
    results = run(db, None, TODAY, say=said.append)
    assert ran == STEPS  # every step was tried, in order
    assert [n for n, o in results if o.status == "failed"] == [{"_sources": "Sources", "_event_pages": "Event pages",
            "_found_pictures": "Found pictures", "_live_pictures": "Live-event pictures", "_photo_sweep": "Photo sweep",
            "_budget_alert": "Budget alert"}[broken]]
    assert "boom" in [o for n, o in results if o.status == "failed"][0].notes[0]
    assert db.scalar(select(DiscoverySource).where(DiscoverySource.name == "half-done")) is None  # rolled back
    assert said == results  # each row was shown as its step ended


def test_the_command_prints_each_step_and_exits_with_an_error_when_one_failed(monkeypatch, capsys):
    from scripts import discover
    monkeypatch.setattr(sys, "argv", ["discover"])
    def fake_run(db, client, today, only=None, force=False, say=None):
        rows = [("Sources", Outcome(status="ok")), ("Found pictures", Outcome(status="failed", notes=["boom"]))]
        for r in rows:
            say(r)
        return rows
    monkeypatch.setattr(discover, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        discover.main()
    out = capsys.readouterr()
    assert e.value.code == 1 and "Sources: ok" in out.out and "Found pictures: failed" in out.out and "Found pictures" in out.err


# ---- criterion 4: a look is counted before the fetch --------------------------------------------------------

def test_a_found_items_look_that_is_killed_still_used_up_a_try(db, monkeypatch):
    s = make_source(db)
    record(db, s, FoundEvent(title="Hamlet", url="https://goodman.example/shows/hamlet", kind="production", category="Theater",
                             begin_date="2027-02-01", end_date="2027-03-01"), "")
    db.commit()
    item = db.scalar(select(DiscoveredItem))
    monkeypatch.setattr(pictures, "add_item_picture", lambda *a, **k: (_ for _ in ()).throw(SystemExit("killed")))
    for n in (1, 2, 3):
        with pytest.raises(SystemExit):
            pictures.look_for_item(db, item, NOW)
        db.rollback(); db.refresh(item)  # what the database kept after the process died
        assert item.image_tries == n
    assert "Gave up after 3 tries" in item.image_note
    assert pictures.due_items(db, NOW + timedelta(days=5)) == []  # left alone


def test_a_live_events_look_that_is_killed_still_used_up_a_try(db, make_event, monkeypatch):
    s = make_source(db)
    ev = make_event(title="Live")
    ev.found_by_id = s.id
    db.commit()
    monkeypatch.setattr(pictures, "add_picture", lambda *a, **k: (_ for _ in ()).throw(SystemExit("killed")))
    with pytest.raises(SystemExit):
        pictures.add_pictures(db, NOW)
    db.rollback(); db.refresh(ev)
    assert ev.picture_tries == 1


# ---- criteria 5 to 7: decoding -------------------------------------------------------------------------------

def _img(fmt, size, mode="RGB"):
    out = io.BytesIO()
    Image.new(mode, size, (200, 60, 40)).save(out, fmt)
    return out.getvalue()


def test_a_big_jpeg_is_shrunk_while_it_is_decoded(monkeypatch):
    from PIL import JpegImagePlugin
    asked = []
    real = JpegImagePlugin.JpegImageFile.draft
    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", lambda self, mode, size: (asked.append(size), real(self, mode, size))[1])
    webp = event_images.normalise(_img("JPEG", (5220, 3414)))
    im = Image.open(io.BytesIO(webp))
    assert asked and max(im.size) == event_images.MAX_SIDE and im.size == (1200, 785)  # the same size as before


@pytest.mark.parametrize("fmt,size,ok", [("WEBP", (2800, 2800), True), ("WEBP", (3000, 3000), False),
                                         ("PNG", (3400, 3400), True), ("PNG", (3600, 3400), False),
                                         ("GIF", (3600, 3400), False)])
def test_formats_that_cannot_be_shrunk_while_decoding_have_a_lower_pixel_limit(fmt, size, ok):
    data = _img(fmt, size, "P" if fmt == "GIF" else "RGB")
    if ok:
        assert max(Image.open(io.BytesIO(event_images.normalise(data))).size) == event_images.MAX_SIDE
    else:
        with pytest.raises(event_images.BadImage, match="too many pixels.*million"):
            event_images.normalise(data)


def test_a_small_orientated_jpeg_still_comes_out_upright():
    ex = Image.Exif(); ex[0x0112] = 6
    out = io.BytesIO()
    Image.new("RGB", (1600, 800), (1, 2, 3)).save(out, "JPEG", exif=ex)
    assert Image.open(io.BytesIO(event_images.normalise(out.getvalue()))).size == (600, 1200)  # turned upright, then shrunk to 1200 as before


def test_a_jpeg_with_a_second_picture_inside_is_still_a_jpeg():  # review: Pillow opens these as MPO, which main accepted up to 50 million
    out = io.BytesIO()
    a = Image.new("RGB", (4032, 3024), (9, 9, 9))
    a.save(out, "MPO", save_all=True, append_images=[Image.new("RGB", (400, 300))])
    data = out.getvalue()
    assert Image.open(io.BytesIO(data)).format == "MPO"
    assert Image.open(io.BytesIO(event_images.normalise(data))).size == (1200, 900)


def test_a_very_wide_jpeg_that_cannot_shrink_far_meets_the_whole_decode_limit():
    with pytest.raises(event_images.TooBig, match="too many pixels"):
        event_images.normalise(_img("JPEG", (30000, 1000)))  # draft can't go below the 1200-pixel side: still 30 million after it


def test_a_sideways_big_jpeg_is_turned_upright_after_shrinking():
    ex = Image.Exif(); ex[0x0112] = 6
    out = io.BytesIO()
    Image.new("RGB", (5220, 3414), (1, 2, 3)).save(out, "JPEG", exif=ex)
    assert Image.open(io.BytesIO(event_images.normalise(out.getvalue()))).size == (785, 1200)


def test_an_admin_is_told_when_a_picture_is_too_big(client, admin_token, make_event):
    from .conftest import auth
    ev = make_event("Hamlet")
    r = client.put(f"/events/{ev.id}/image", content=_img("PNG", (3600, 3400)),
                   headers={**auth(admin_token), "Content-Type": "image/png"})
    assert r.status_code == 422 and "too big" in r.json()["detail"] and "12 million" in r.json()["detail"]
    r = client.put(f"/events/{ev.id}/image", content=b"not a png", headers={**auth(admin_token), "Content-Type": "image/png"})
    assert r.status_code == 422 and "too big" not in r.json()["detail"]
