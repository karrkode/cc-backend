"""CC-265: admins can replace event artwork or return an event to its default water."""
import io

import pytest
from PIL import Image

from app.config import get_settings
from app.discovery import pictures
from app.models import DiscoverySource, SourceKind

from .conftest import auth


@pytest.fixture
def image_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_IMAGE_DIR", str(tmp_path))
    monkeypatch.setenv("EVENT_IMAGE_BACKEND", "local")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def picture(fmt="JPEG", colour=(200, 60, 40)):
    out = io.BytesIO()
    Image.new("RGB", (400, 250), colour).save(out, fmt)
    return out.getvalue()


def send(client, event_id, token, content=None, kind="image/jpeg"):
    return client.put(f"/events/{event_id}/image", content=picture() if content is None else content,
                      headers={**auth(token), "Content-Type": kind})


def test_admin_can_replace_and_remove_picture(client, admin_token, make_event, image_folder, db):
    ev = make_event("Hamlet")
    h = auth(admin_token)
    first = send(client, ev.id, admin_token)
    assert first.status_code == 200, first.text
    first_url = first.json()["image_url"]
    assert first_url.endswith(".webp") and first.json()["image_credit_url"] is None
    db.refresh(ev)
    assert (image_folder / ev.image_key).is_file()
    assert (image_folder / "originals" / str(ev.id)).is_dir()
    assert ev.image_override
    assert client.get(f"/events/{ev.id}").json()["image_url"] == first_url
    assert {i["id"]: i for i in client.get("/events").json()["items"]}[ev.id]["image_url"] == first_url

    changed = send(client, ev.id, admin_token, picture(colour=(30, 50, 180)))
    assert changed.status_code == 200, changed.text
    assert changed.json()["image_url"] != first_url
    assert client.get(f"/events/{ev.id}").json()["image_url"] == changed.json()["image_url"]

    removed = client.delete(f"/events/{ev.id}/image", headers=h)
    assert removed.status_code == 200, removed.text
    assert removed.json()["image_url"] is None and removed.json()["image_credit_url"] is None
    assert client.get(f"/events/{ev.id}").json()["image_url"] is None
    db.refresh(ev)
    assert ev.image_key is None and ev.image_override and ev.image_source_url == ""


def test_manual_removal_is_not_reversed_by_sourcing(client, admin_token, make_event, db, image_folder):
    source = DiscoverySource(name="Photo source", kind=SourceKind.theater,
                             pages=["https://example.org/season"], reader="claude",
                             venue_name="Goodman Theatre", neighborhood="Loop", every_days=30,
                             page_state={}, venue_hours={}, unchanged_streak=0,
                             last_status="", last_error="")
    db.add(source)
    db.flush()
    ev = make_event("Hamlet")
    ev.found_by_id = source.id
    db.commit()
    assert pictures.due(db, pictures.datetime.now(pictures.UTC)) == [ev]
    response = client.delete(f"/events/{ev.id}/image", headers=auth(admin_token))
    assert response.status_code == 200
    db.refresh(ev)
    assert ev.picture_tries == 0  # a separate flag, not a pretend exhausted retry count
    assert pictures.due(db, pictures.datetime.now(pictures.UTC)) == []
    with pytest.raises(pictures.NoPicture, match="admin"):
        pictures.add_picture(db, ev)


def test_only_admin_may_modify_artwork(client, admin_token, user_token, make_event, image_folder):
    ev = make_event("Hamlet")
    assert send(client, ev.id, user_token).status_code == 403
    assert client.delete(f"/events/{ev.id}/image", headers=auth(user_token)).status_code == 403
    assert client.put(f"/events/{ev.id}/image", content=picture(), headers={"Content-Type": "image/jpeg"}).status_code == 401
    assert client.delete(f"/events/{ev.id}/image").status_code == 401
    assert send(client, 987654, admin_token).status_code == 404
    assert client.delete("/events/987654/image", headers=auth(admin_token)).status_code == 404


def test_bad_upload_does_not_change_existing_picture(client, admin_token, make_event, image_folder, db):
    ev = make_event("Hamlet")
    original = send(client, ev.id, admin_token)
    assert original.status_code == 200
    url = original.json()["image_url"]
    bad = send(client, ev.id, admin_token, b"<svg onload='alert(1)'/>", "image/svg+xml")
    assert bad.status_code == 415
    bad = send(client, ev.id, admin_token, b"not a jpeg", "image/jpeg")
    assert bad.status_code == 422
    bad = send(client, ev.id, admin_token, picture(), "image/png")  # bytes disagree with the header
    assert bad.status_code == 422
    bad = send(client, ev.id, admin_token, b"x" * 3_800_001, "image/jpeg")
    assert bad.status_code == 413
    db.refresh(ev)
    assert ev.image_url == url and ev.image_override
    assert client.get(f"/events/{ev.id}").json()["title"] == "Hamlet"
