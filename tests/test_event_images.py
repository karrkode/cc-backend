"""CC-250: event photos: normalised to WebP, stored by a hash of their bytes, and shown as image_url."""
import io
import json

import pytest
from PIL import Image

from app import event_images
from app.config import get_settings
from scripts import event_images as importer


def _image(fmt, size=(2400, 1600), mode="RGB", exif=False):
    im = Image.new(mode, size, (200, 60, 40, 128) if mode == "RGBA" else (200, 60, 40))
    out = io.BytesIO()
    kw = {}
    if exif:
        ex = Image.Exif()
        ex[0x0112] = 6  # rotated 90°: the camera held sideways
        ex[0x010F] = "SomeCamera"
        kw["exif"] = ex
    im.save(out, fmt, **kw)
    return out.getvalue()


@pytest.fixture
def local_images(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_IMAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.mark.parametrize("fmt,mode", [("JPEG", "RGB"), ("PNG", "RGBA"), ("WEBP", "RGB"), ("AVIF", "RGB"), ("GIF", "P")])
def test_every_format_becomes_a_small_webp(fmt, mode):
    webp = event_images.normalise(_image(fmt, mode=mode))
    im = Image.open(io.BytesIO(webp))
    assert im.format == "WEBP"
    assert max(im.size) == event_images.MAX_SIDE and im.size == (1200, 800)  # the shape is kept


def test_small_images_are_not_enlarged():
    assert Image.open(io.BytesIO(event_images.normalise(_image("JPEG", (300, 300))))).size == (300, 300)


def test_sideways_photos_turn_upright_and_lose_their_metadata():
    webp = event_images.normalise(_image("JPEG", (1600, 1200), exif=True))
    im = Image.open(io.BytesIO(webp))
    assert im.size == (900, 1200)
    assert not im.getexif() and "icc_profile" not in im.info


def test_not_an_image_is_refused():
    with pytest.raises(event_images.BadImage):
        event_images.normalise(b"<html>not a picture</html>")


def test_save_keeps_the_original_and_names_the_webp_by_its_bytes(local_images):
    data = _image("PNG")
    key = event_images.save(7, data, ".png")
    assert key.startswith("event-images/7/") and key.endswith(".webp")
    assert key == event_images.save(7, data, ".png")  # the same photo again: the same key, nothing new
    assert (local_images / key).is_file()
    assert [p.name.endswith(".png") for p in (local_images / "originals" / "7").iterdir()] == [True]


def test_api_gives_the_photo_address_or_null(client, db, make_event):
    with_photo, without = make_event("Hamlet"), make_event("Macbeth")
    with_photo.image_key = "event-images/1/abc.webp"
    db.commit()
    items = {e["title"]: e for e in client.get("/events").json()["items"]}
    assert items["Hamlet"]["image_url"] == f"{get_settings().site_url}/event-images/1/abc.webp"
    assert items["Macbeth"]["image_url"] is None
    assert items["Hamlet"]["url"] == "https://example.org/hamlet"  # the venue page stays separate
    assert client.get(f"/events/{without.id}").json()["image_url"] is None


def test_import_fills_only_empty_events_and_skips_unknown_ones(db, make_event, tmp_path, local_images):
    a, b = make_event("Hamlet"), make_event("Macbeth")
    b.image_key = "event-images/x/kept.webp"
    db.commit()
    folder = tmp_path / "photos"
    folder.mkdir()
    (folder / "hamlet.jpg").write_bytes(_image("JPEG"))
    (folder / "macbeth.webp").write_bytes(_image("WEBP"))
    (folder / "gone.png").write_bytes(_image("PNG"))
    mapping = tmp_path / "mapping.csv"
    mapping.write_text("event_id,file,source_url\n"
                       f"{a.id},hamlet.jpg,https://example.org/hamlet\n{b.id},macbeth.webp,\n99999,gone.png,\n")
    manifest = tmp_path / "manifest.json"
    importer.upload(mapping, folder, manifest)
    assert [m["event_id"] for m in json.loads(manifest.read_text())] == sorted([a.id, b.id, 99999])

    assert importer.apply(manifest, dry_run=True, db=db) == {"set": 1, "has_one": 1, "no_event": 1}
    db.refresh(a)
    assert a.image_key is None  # a dry run changes nothing

    assert importer.apply(manifest, db=db) == {"set": 1, "has_one": 1, "no_event": 1}
    db.refresh(a); db.refresh(b)
    assert a.image_key.startswith(f"event-images/{a.id}/") and a.image_source_url == "https://example.org/hamlet"
    assert b.image_key == "event-images/x/kept.webp"  # never replaced
    assert importer.apply(manifest, db=db)["set"] == 0  # safe to run again


def test_a_mapping_that_gives_an_event_two_photos_is_refused(tmp_path, local_images):
    mapping = tmp_path / "mapping.csv"
    mapping.write_text("event_id,file,source_url\n1,a.jpg,\n1,b.jpg,\n")
    with pytest.raises(SystemExit):
        importer.upload(mapping, tmp_path, tmp_path / "m.json")
