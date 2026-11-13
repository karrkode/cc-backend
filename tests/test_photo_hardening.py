"""CC-197: photos are decoded and re-saved without metadata, the daily limit holds under parallel uploads, a
clash leaves no file behind, and two decisions on one photo can't both win."""
import io
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import photos
from app.models import Attended, OwnEvent, PhotoCheck, WentPhoto

from .conftest import IS_POSTGRES, auth
from .test_own_journal_events import add
from .test_went_photos import been, files, picture, stored, upload  # noqa: F401 (stored is a fixture)

needs_postgres = pytest.mark.skipif(not IS_POSTGRES, reason="SQLite in tests shares one connection; Postgres checks the race")


@pytest.fixture
def seen(stored, monkeypatch):  # noqa: F811
    """What Rekognition was sent."""
    root, found = stored
    sent = []
    monkeypatch.setattr(photos, "_rekognition", lambda data: sent.append(data) or [])
    return root, sent


def only_file(root):
    (name,) = files(root)
    return (root / "photos" / name).read_bytes()


def test_metadata_is_removed_and_the_orientation_applied(client, user_token, make_event, seen):
    root, sent = seen
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    original = picture("JPEG", size=(40, 20), exif=True, icc=True)
    assert Image.open(io.BytesIO(original)).getexif().get(0x010F) == "TestCam"
    assert upload(client, user_token, ev.id, original).status_code == 200
    kept = only_file(root)
    im = Image.open(io.BytesIO(kept))
    assert im.format == "JPEG" and im.size == (20, 40)  # orientation 6 turns it upright
    assert len(im.getexif()) == 0 and "icc_profile" not in im.info and b"TestCam" not in kept
    assert sent == [kept]  # Rekognition saw the very bytes we keep


@pytest.mark.parametrize("kw", [{"comment": b"SECRET-COMMENT"}, {"comment": b"SECRET-COMMENT", "exif": True},
                                {"comment": b"SECRET-COMMENT", "mode": "L", "colour": (90,)}])
def test_a_jpeg_comment_is_removed_too(client, user_token, make_event, seen, kw):
    root, _ = seen
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    assert b"SECRET-COMMENT" in picture("JPEG", **kw)
    assert upload(client, user_token, ev.id, picture("JPEG", **kw)).status_code == 200
    assert b"SECRET-COMMENT" not in only_file(root)


def test_a_phone_sized_picture_is_taken_and_decodes_that_wait_too_long_are_turned_away(client, user_token, make_event, db, seen, monkeypatch):
    root, sent = seen
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    assert photos.MAX_PIXELS >= 4032 * 3024
    monkeypatch.setattr(photos, "DECODE_WAIT", 0.01)
    for _ in range(2):  # both decoders taken
        assert photos.DECODERS.acquire(timeout=1)
    try:
        r = upload(client, user_token, ev.id)
    finally:
        photos.DECODERS.release()
        photos.DECODERS.release()
    assert r.status_code == 503 and files(root) == [] and sent == [] and db.scalar(select(PhotoCheck)) is None


def test_a_webp_comes_back_as_a_jpeg_and_a_big_one_is_shrunk(client, user_token, make_event, seen):
    root, _ = seen
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    r = upload(client, user_token, ev.id, picture("WEBP", size=(3200, 800), mode="RGBA"), "image/webp")
    assert r.status_code == 200
    im = Image.open(io.BytesIO(only_file(root)))
    assert im.format == "JPEG" and im.mode == "RGB" and im.size == (1600, 400)
    assert files(root)[0].endswith(".jpg")


def test_an_unreadable_or_oversized_picture_is_refused_before_anything_is_spent(client, user_token, make_event, db, seen, monkeypatch):
    root, sent = seen
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    assert upload(client, user_token, ev.id, b"\xff\xd8\xff" + b"garbage" * 50).status_code == 415
    assert upload(client, user_token, ev.id, b"RIFF\x00\x00\x00\x00WEBP" + b"junk" * 50, "image/webp").status_code == 415
    monkeypatch.setattr(photos, "MAX_PIXELS", 500)
    assert upload(client, user_token, ev.id, picture("JPEG", size=(40, 20))).status_code == 415  # 800 pixels
    assert files(root) == [] and sent == [] and db.scalar(select(PhotoCheck)) is None


def test_an_own_events_photo_is_cleaned_too(client, user_token, db, seen):
    root, sent = seen
    ev = add(client, user_token).json()
    r = client.put(f"/me/own-events/{ev['id']}/photo", content=picture("JPEG", exif=True, icc=True),
                   headers={**auth(user_token), "Content-Type": "image/jpeg"})
    assert r.status_code == 200
    kept = only_file(root)
    assert len(Image.open(io.BytesIO(kept)).getexif()) == 0 and sent == [kept]
    assert db.scalar(select(PhotoCheck.outcome)) == "stored"


def test_a_mark_removed_during_the_check_gives_a_409_and_no_file(client, user_token, make_event, db, Session, stored, monkeypatch):
    root, _ = stored
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)

    def removes_the_mark(data):
        with Session() as other:
            other.execute(delete(Attended))
            other.commit()
        return []
    monkeypatch.setattr(photos, "_rekognition", removes_the_mark)
    r = upload(client, user_token, ev.id)
    assert r.status_code == 409 and "changed at the same time" in r.json()["detail"]
    db.expire_all()
    assert files(root) == [] and db.scalar(select(WentPhoto)) is None
    assert [c.outcome for c in db.scalars(select(PhotoCheck))] == ["failed"]  # Rekognition ran, so it counts


def test_a_storage_failure_after_the_check_counts_and_leaves_nothing(client, user_token, make_event, db, stored, monkeypatch):
    root, _ = stored
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)

    def broken(self, key, data, content_type):
        raise OSError("disk full")
    monkeypatch.setattr(photos.LocalStore, "put", broken)
    assert upload(client, user_token, ev.id).status_code == 503
    assert files(root) == [] and [c.outcome for c in db.scalars(select(PhotoCheck))] == ["failed"]


def test_a_replaced_photo_keeps_its_old_file_when_the_swap_fails_to_commit(client, user_token, make_event, db, stored, monkeypatch):
    root, _ = stored
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    assert upload(client, user_token, ev.id).status_code == 200
    old = files(root)
    real_put, real_commit = photos.LocalStore.put, Session.commit
    armed = []

    def put_then_arm(self, key, data, content_type):
        real_put(self, key, data, content_type)
        armed.append(1)  # the new file has landed: the next commit (the swap) fails
    monkeypatch.setattr(photos.LocalStore, "put", put_then_arm)

    def commit(self):
        if armed:
            armed.clear()
            raise OSError("database went away")
        return real_commit(self)
    monkeypatch.setattr(Session, "commit", commit)
    assert upload(client, user_token, ev.id).status_code == 503
    assert files(root) == old  # the new file was deleted; the old one is kept, as is its row
    db.expire_all()
    assert db.scalar(select(WentPhoto.key)) == f"photos/{old[0]}"
    assert sorted(c.outcome for c in db.scalars(select(PhotoCheck))) == ["failed", "stored"]


# ---- parallel (Postgres only) -----------------------------------------------------------------

def upload_in_parallel(client, jobs):
    with ThreadPoolExecutor(20) as pool:
        return list(pool.map(lambda job: job(), jobs))


@needs_postgres
def test_parallel_uploads_never_pass_the_daily_limit(client, user_token, make_event, db, engine, seen, monkeypatch):
    root, sent = seen
    count = photos.uploads_today

    def slow_count(db, user_id):  # a wide gap between counting and recording, so a missing lock shows
        n = count(db, user_id)
        time.sleep(0.1)
        return n
    monkeypatch.setattr(photos, "uploads_today", slow_count)
    ev1, ev2 = make_event("Hamlet"), make_event("Macbeth")
    own = add(client, user_token).json()
    for ev in (ev1, ev2):
        been(client, user_token, ev.id)
    body = picture("JPEG", size=(60, 40))
    jobs = [lambda ev=ev1: upload(client, user_token, ev.id, body).status_code for _ in range(10)]
    jobs += [lambda: upload(client, user_token, ev2.id, body).status_code for _ in range(6)]
    jobs += [lambda: client.put(f"/me/own-events/{own['id']}/photo", content=body,
                                headers={**auth(user_token), "Content-Type": "image/jpeg"}).status_code for _ in range(4)]
    try:
        codes = upload_in_parallel(client, jobs)
    finally:
        engine.dispose()
    db.expire_all()
    assert codes.count(200) <= photos.UPLOADS_PER_DAY and set(codes) <= {200, 409, 429} and codes.count(200) >= 1
    assert db.scalar(select(PhotoCheck.id).where(PhotoCheck.outcome == "checking")) is None
    assert len(list(db.scalars(select(PhotoCheck)))) <= photos.UPLOADS_PER_DAY and len(sent) <= photos.UPLOADS_PER_DAY
    known = set(db.scalars(select(WentPhoto.key)))
    known |= {k for k in db.scalars(select(OwnEvent.photo_key)) if k}
    assert sorted(f"photos/{n}" for n in files(root)) == sorted(known)  # nothing orphaned, nothing missing


@needs_postgres
def test_two_decisions_on_one_photo_never_both_win(client, user_token, admin_token, make_event, db, engine, stored, monkeypatch):
    root, _ = stored

    class SlowClock(datetime):  # a wide gap between reading the photo and committing, so a missing lock shows
        @classmethod
        def now(cls, tz=None):
            time.sleep(0.2)
            return super().now(tz)
    monkeypatch.setattr("app.routers.approvals.datetime", SlowClock)
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    try:
        for _ in range(4):
            assert upload(client, user_token, ev.id).status_code == 200
            item = client.get("/approvals", headers=auth(admin_token)).json()["items"][0]
            decide = lambda d: client.post(f"/approvals/{item['id']}", json={"decision": d}, headers=auth(admin_token))
            with ThreadPoolExecutor(2) as pool:
                a, b = pool.map(decide, ["approve", "reject"])
            assert sorted([a.status_code, b.status_code]) == [200, 409]
            won = a if a.status_code == 200 else b
            db.expire_all()
            row = db.get(WentPhoto, item["id"])
            assert row.status == won.json()["status"]
            assert len(files(root)) == (1 if row.status == "approved" else 0)
    finally:
        engine.dispose()
