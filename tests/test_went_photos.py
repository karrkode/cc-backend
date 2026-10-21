"""CC-76: a photo with "I've been": checked by Amazon Rekognition (mocked here), stored privately, shown
through short-lived signed links, and removed with its mark or event."""
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import photos
from app.config import get_settings
from app.models import PhotoCheck, User, WentPhoto

from .conftest import auth

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2000  # enough of a JPEG for the type check
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 500


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """Photos go to a temporary folder; Rekognition answers with whatever labels the test sets."""
    monkeypatch.setattr(get_settings(), "photo_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "photo_checker", "rekognition")
    found = {"labels": []}
    monkeypatch.setattr(photos, "_rekognition", lambda data: found["labels"])
    return tmp_path, found


def files(root):
    return sorted(p.name for p in (root / "photos").glob("*")) if (root / "photos").exists() else []


def been(client, token, event_id):
    assert client.put(f"/me/attended/{event_id}", headers=auth(token)).status_code in (200, 201)


def upload(client, token, event_id, data=JPEG, content_type="image/jpeg"):
    return client.put(f"/me/attended/{event_id}/photo", content=data, headers={**auth(token), "Content-Type": content_type})


def test_a_photo_is_checked_stored_privately_and_shown_by_signed_link(client, user_token, admin_token, make_event, stored):
    root, found = stored
    ev = make_event("Hamlet")
    assert upload(client, user_token, ev.id).status_code == 404  # mark it "I've been" first
    been(client, user_token, ev.id)
    assert upload(client, user_token, ev.id, b"not a photo").status_code == 415
    assert upload(client, user_token, ev.id, JPEG, "image/png").status_code == 415
    assert upload(client, user_token, ev.id, b"\xff\xd8\xff" + b"0" * photos.MAX_BYTES).status_code == 413

    r = upload(client, user_token, ev.id)
    assert r.status_code == 200 and r.json()["status"] == "waiting"
    assert len(files(root)) == 1
    mine = client.get("/me/attended", headers=auth(user_token)).json()[0]["photo"]
    assert mine["status"] == "waiting" and mine["url"].startswith(f"/photos/{mine['id']}?exp=")

    shown = client.get(mine["url"])  # no sign-in needed: the link is the permission
    assert shown.status_code == 200 and shown.content == JPEG and shown.headers["content-type"] == "image/jpeg"
    assert shown.headers["cache-control"] == "private, max-age=600" and shown.headers["x-content-type-options"] == "nosniff"
    assert client.get(mine["url"].replace("sig=", "sig=0")).status_code == 404
    past = int(time.time()) - 1
    assert client.get(f"/photos/{mine['id']}?exp={past}&sig={photos._sig(mine['id'], past)}").status_code == 404

    # someone else never sees it in their list
    assert client.get("/me/attended", headers=auth(admin_token)).json() == []

    # a new one replaces it, and WebP works too
    r = upload(client, user_token, ev.id, WEBP, "image/webp")
    assert r.status_code == 200 and len(files(root)) == 1 and files(root)[0].endswith(".webp")


def test_rekognition_refuses_unsafe_photos_and_blurs_borderline_ones(client, user_token, make_event, db, stored):
    root, found = stored
    ev = make_event("Club Night")
    been(client, user_token, ev.id)
    found["labels"] = [{"Name": "Explicit Nudity", "ParentName": "", "Confidence": 97.2}]
    r = upload(client, user_token, ev.id)
    assert r.status_code == 422 and r.json()["detail"] == "This photo can't be used. Try another."
    assert files(root) == [] and db.scalar(select(WentPhoto)) is None  # never stored
    assert [c.outcome for c in db.scalars(select(PhotoCheck))] == ["refused"]

    found["labels"] = [{"Name": "Explicit Nudity", "ParentName": "", "Confidence": 70.0}]  # not sure enough to refuse
    assert upload(client, user_token, ev.id).status_code == 200
    photo = db.scalar(select(WentPhoto))
    assert photo.blurred and photo.labels == [{"name": "Explicit Nudity", "parent": "", "confidence": 70.0}]

    found["labels"] = [{"Name": "Alcoholic Beverages", "ParentName": "Alcohol", "Confidence": 88.0}]
    upload(client, user_token, ev.id)
    db.expire_all()
    assert db.scalar(select(WentPhoto)).blurred  # milder labels blur it for approvers

    found["labels"] = []
    upload(client, user_token, ev.id)
    db.expire_all()
    assert not db.scalar(select(WentPhoto)).blurred and len(files(root)) == 1


def test_an_unreachable_rekognition_stores_nothing(client, user_token, make_event, db, stored, monkeypatch):
    root, _ = stored
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)

    def down(data):
        raise ConnectionError("no route to Rekognition")
    monkeypatch.setattr(photos, "_rekognition", down)
    r = upload(client, user_token, ev.id)
    assert r.status_code == 503 and r.json()["detail"] == "Photos can't be sent right now. Try again later."
    assert files(root) == [] and db.scalar(select(WentPhoto)) is None and db.scalar(select(PhotoCheck)) is None


def test_ten_uploads_a_day(client, user_token, make_event, db, stored):
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    for _ in range(photos.UPLOADS_PER_DAY):
        assert upload(client, user_token, ev.id).status_code == 200
    r = upload(client, user_token, ev.id)
    assert r.status_code == 429 and "Try again tomorrow" in r.json()["detail"]
    for c in db.scalars(select(PhotoCheck)):
        c.checked_at = datetime.now(UTC) - timedelta(days=2)
    db.commit()
    assert upload(client, user_token, ev.id).status_code == 200


def test_removing_the_mark_or_the_event_removes_the_photo(client, user_token, admin_token, make_event, db, stored):
    root, _ = stored
    a, b = make_event("Hamlet"), make_event("Macbeth")
    for ev in (a, b):
        been(client, user_token, ev.id)
        upload(client, user_token, ev.id)
    assert len(files(root)) == 2
    assert client.delete(f"/me/attended/{a.id}", headers=auth(user_token)).status_code == 204
    assert len(files(root)) == 1
    assert client.delete(f"/events/{b.id}", headers=auth(admin_token)).status_code == 204
    assert files(root) == [] and db.scalar(select(WentPhoto)) is None

    been(client, user_token, a.id)
    upload(client, user_token, a.id)
    assert client.delete(f"/me/attended/{a.id}/photo", headers=auth(user_token)).status_code == 204
    assert files(root) == [] and client.get("/me/attended", headers=auth(user_token)).json()[0]["photo"] is None


def test_the_sweep_removes_photos_the_database_no_longer_knows(client, user_token, make_event, db, stored):
    root, _ = stored
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    upload(client, user_token, ev.id)
    (root / "photos" / "left-behind.jpg").write_bytes(JPEG)
    (root / "photos" / "being-saved.jpg").write_bytes(JPEG)
    hour_ago = time.time() - 3 * 3600
    os.utime(root / "photos" / "left-behind.jpg", (hour_ago, hour_ago))
    assert photos.sweep(db) == 1
    assert "left-behind.jpg" not in files(root) and "being-saved.jpg" in files(root) and len(files(root)) == 2

    # an account deleted straight from the database takes its marks and photos with it; the sweep tidies the file
    user = db.scalar(select(User).where(User.id == db.scalar(select(WentPhoto.user_id))))
    db.delete(user)
    db.commit()
    assert db.scalar(select(WentPhoto)) is None
    for p in (root / "photos").glob("*"):
        os.utime(p, (hour_ago, hour_ago))
    assert photos.sweep(db) == 2 and files(root) == []


def test_site_stats_show_rekognitions_cost_this_month(client, user_token, admin_token, make_event, stored):
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    for _ in range(3):
        upload(client, user_token, ev.id)
    s = client.get("/admin/stats", headers=auth(admin_token)).json()
    assert (s["photo_checks_month"], s["photo_checks_usd"]) == (3, 0.0)  # $0.003 rounds to nothing yet


def test_locally_nothing_is_checked(monkeypatch):
    monkeypatch.setattr(get_settings(), "photo_checker", "")
    assert get_settings().photo_moderation == "none" and photos.moderation_labels(JPEG) == []
    monkeypatch.setattr(get_settings(), "env", "prod")
    assert get_settings().photo_moderation == "rekognition" and get_settings().photo_storage == "s3"


def test_s3_storage_is_private_and_encrypted(monkeypatch):
    calls = []

    class FakeS3:
        def put_object(self, **kw):
            calls.append(("put", kw))

        def delete_object(self, **kw):
            calls.append(("delete", kw))

    s3 = photos.S3Store("chiculturecrew-went-photos", "us-east-2")
    monkeypatch.setattr(s3, "_s3", lambda: FakeS3())
    s3.put("photos/a.jpg", JPEG, "image/jpeg")
    s3.delete("photos/a.jpg")
    put = calls[0][1]
    assert put["Bucket"] == "chiculturecrew-went-photos" and put["ServerSideEncryption"] == "AES256"
    assert "ACL" not in put and calls[1] == ("delete", {"Bucket": "chiculturecrew-went-photos", "Key": "photos/a.jpg"})
