"""CC-248: a member's own journal events (not in the catalog) and own categories, private to them."""
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import photos
from app.config import get_settings
from app.models import OwnEvent, PhotoCheck
from app.routers import journal
from app.stats import today_in_chicago

from .conftest import auth, register

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2000


@pytest.fixture(autouse=True)
def _limits():
    journal.write_limiter.reset()


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "photo_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "photo_checker", "rekognition")
    found = {"labels": []}
    monkeypatch.setattr(photos, "_rekognition", lambda data: found["labels"])
    return tmp_path, found


def files(root):
    return sorted(p.name for p in (root / "photos").glob("*")) if (root / "photos").exists() else []


def add(client, token, **body):
    body = {"title": "Open mic at Cole's", "date": str(today_in_chicago()), **body}
    return client.post("/me/own-events", json=body, headers=auth(token))


def test_add_an_own_event_with_just_a_title_and_date(client, user_token):
    r = add(client, user_token, title="  Open   mic  ", venue=" Cole's  Bar ")
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["title"] == "Open mic" and ev["venue"] == "Cole's Bar" and ev["words"] == ""
    assert ev["category"] is None and ev["category_id"] is None and ev["photo_url"] == ""
    assert ev["date"] == str(today_in_chicago())


def test_future_and_past_dates_are_fine_within_reason(client, user_token):
    today = today_in_chicago()
    assert add(client, user_token, date=str(today + timedelta(days=30))).status_code == 201
    assert add(client, user_token, date=str(today - timedelta(days=3000))).status_code == 201
    assert add(client, user_token, date="1900-01-01").status_code == 422
    assert add(client, user_token, date=str(today + timedelta(days=800))).status_code == 422
    dates = [e["date"] for e in client.get("/me/own-events", headers=auth(user_token)).json()]
    assert dates == sorted(dates, reverse=True)  # latest first


def test_checks_on_what_it_holds(client, user_token):
    assert add(client, user_token, title="   ").status_code == 422
    assert add(client, user_token, title="x" * 121).status_code == 422
    assert add(client, user_token, venue="x" * 121).status_code == 422
    assert add(client, user_token, words="only two").status_code == 422
    assert add(client, user_token, words="a " + "x" * 21 + " c").status_code == 422
    assert add(client, user_token, category="Comedy").status_code == 422  # the catalog keeps its three
    r = add(client, user_token, words=" loud   sweaty  joyful ", review="Great night.", recommend=True, category="Music")
    assert r.status_code == 201 and r.json()["words"] == "loud sweaty joyful" and r.json()["category_name"] == "Music"


def test_change_and_delete_and_nobody_else_can(client, user_token):
    ev = add(client, user_token).json()
    other = register(client, email="bo@example.com", name="Bo")
    body = {"title": "Poetry slam", "date": ev["date"], "words": "fierce funny brave"}
    assert client.put(f"/me/own-events/{ev['id']}", json=body, headers=auth(other)).status_code == 404
    assert client.get("/me/own-events", headers=auth(other)).json() == []
    r = client.put(f"/me/own-events/{ev['id']}", json=body, headers=auth(user_token))
    assert r.status_code == 200 and r.json()["title"] == "Poetry slam" and r.json()["words"] == "fierce funny brave"
    assert client.delete(f"/me/own-events/{ev['id']}", headers=auth(other)).status_code == 204
    assert len(client.get("/me/own-events", headers=auth(user_token)).json()) == 1  # still there
    assert client.delete(f"/me/own-events/{ev['id']}", headers=auth(user_token)).status_code == 204
    assert client.get("/me/own-events", headers=auth(user_token)).json() == []
    assert client.get("/me/own-events").status_code == 401


def test_own_categories(client, user_token):
    r = client.post("/me/categories", json={"name": " Street  art "}, headers=auth(user_token))
    assert r.status_code == 201 and r.json()["name"] == "Street art"
    cat = r.json()
    assert client.post("/me/categories", json={"name": "street ART"}, headers=auth(user_token)).status_code == 409
    assert client.post("/me/categories", json={"name": "  "}, headers=auth(user_token)).status_code == 422
    assert client.post("/me/categories", json={"name": "x" * 31}, headers=auth(user_token)).status_code == 422

    ev = add(client, user_token, category_id=cat["id"]).json()
    assert ev["category_id"] == cat["id"] and ev["category_name"] == "Street art"
    assert add(client, user_token, category="Music", category_id=cat["id"]).status_code == 422  # one or the other
    other = register(client, email="bo@example.com", name="Bo")
    assert add(client, other, category_id=cat["id"]).status_code == 404  # not theirs
    assert client.get("/me/categories", headers=auth(other)).json() == []

    listed = client.get("/me/categories", headers=auth(user_token)).json()
    assert listed == [{"id": cat["id"], "name": "Street art", "events": 1}]
    r = client.put(f"/me/categories/{cat['id']}", json={"name": "Murals"}, headers=auth(user_token))
    assert r.status_code == 200 and r.json()["events"] == 1
    assert client.put(f"/me/categories/{cat['id']}", json={"name": "x"}, headers=auth(other)).status_code == 404
    assert client.get("/me/own-events", headers=auth(user_token)).json()[0]["category_name"] == "Murals"

    # deleting it keeps its events, uncategorised
    assert client.delete(f"/me/categories/{cat['id']}", headers=auth(other)).status_code == 204
    assert len(client.get("/me/categories", headers=auth(user_token)).json()) == 1
    assert client.delete(f"/me/categories/{cat['id']}", headers=auth(user_token)).status_code == 204
    left = client.get("/me/own-events", headers=auth(user_token)).json()
    assert len(left) == 1 and left[0]["category_id"] is None and left[0]["category_name"] == ""


def test_limits(client, user_token, monkeypatch):
    monkeypatch.setattr(journal, "MAX_EVENTS", 2)
    monkeypatch.setattr(journal, "MAX_CATEGORIES", 1)
    assert add(client, user_token).status_code == 201
    assert add(client, user_token).status_code == 201
    r = add(client, user_token)
    assert r.status_code == 422 and "up to 2" in r.json()["detail"]
    assert client.post("/me/categories", json={"name": "A"}, headers=auth(user_token)).status_code == 201
    assert client.post("/me/categories", json={"name": "B"}, headers=auth(user_token)).status_code == 422

    monkeypatch.setattr(journal, "WRITES_PER_HOUR", 0)
    assert client.post("/me/categories", json={"name": "C"}, headers=auth(user_token)).status_code == 429


def test_own_event_photo_has_no_approver_and_goes_with_the_event(client, user_token, admin_token, db, stored):
    root, found = stored
    ev = add(client, user_token).json()
    url = f"/me/own-events/{ev['id']}/photo"
    put = lambda data=JPEG, token=user_token, ct="image/jpeg": client.put(url, content=data, headers={**auth(token), "Content-Type": ct})

    assert put(b"not a photo").status_code == 415
    assert put(token=admin_token).status_code == 404  # not theirs
    r = put()
    assert r.status_code == 200 and r.json()["photo_url"].startswith(f"/photos/own/{ev['id']}?exp=")
    shown = client.get(r.json()["photo_url"])
    assert shown.status_code == 200 and shown.content == JPEG and shown.headers["cache-control"] == "private, max-age=600"
    assert client.get(r.json()["photo_url"].replace("sig=", "sig=0")).status_code == 404
    exp = int(time.time()) + 60
    assert client.get(f"/photos/{ev['id']}?exp={exp}&sig={photos._sig(ev['id'], exp, photos.OWN)}").status_code == 404
    assert client.get(f"/photos/own/{ev['id']}?exp={exp}&sig={photos._sig(ev['id'], exp)}").status_code == 404
    assert len(files(root)) == 1

    # a new one replaces it; a refused one is never stored and counts toward the day's limit
    assert put().status_code == 200 and len(files(root)) == 1
    found["labels"] = [{"Name": "Explicit Nudity", "ParentName": "", "Confidence": 99.0}]
    r = put()
    assert r.status_code == 422 and len(files(root)) == 1
    assert [c.outcome for c in db.scalars(select(PhotoCheck))] == ["stored", "stored", "refused"]

    found["labels"] = []
    r = client.delete(url, headers=auth(user_token))
    assert r.status_code == 200 and r.json()["photo_url"] == "" and files(root) == []
    assert put().status_code == 200 and len(files(root)) == 1
    assert client.delete(f"/me/own-events/{ev['id']}", headers=auth(user_token)).status_code == 204
    assert files(root) == []


def test_sweep_keeps_own_event_photos(client, user_token, db, stored):
    root, _ = stored
    ev = add(client, user_token).json()
    client.put(f"/me/own-events/{ev['id']}/photo", content=JPEG, headers={**auth(user_token), "Content-Type": "image/jpeg"})
    assert photos.sweep(db, older_than_minutes=-5) == 0 and len(files(root)) == 1
    db.expire_all()
    assert db.scalar(select(OwnEvent.photo_key)).startswith("photos/")
