"""Photos with "I've been" (CC-76), as agreed with the product owner.

- Optional: "I've been" never needs one. One photo per member per event; a new one replaces it.
- The browser shrinks it (longest side about 1600px) and re-encodes it as JPEG. CC-197: the server does it
  again (clean) whatever it's sent, so the stored file is always a JPEG with no location, camera details or
  colour profile, and its orientation applied. The API takes JPEG or WebP only, up to MAX_BYTES, and up to
  UPLOADS_PER_DAY a member.
- Amazon Rekognition (DetectModerationLabels, $0.001 a photo) checks the cleaned photo before it's stored. Explicit,
  graphically violent or hateful content at high confidence is refused and never stored. Anything else
  it finds blurs the photo for approvers (CC-91), with its labels. Every photo waits for an approver.
- Stored privately: an S3 bucket in production (encrypted, no public access), a folder locally. Shown
  only through short-lived links signed by the API, to the member and to admins (approvers: CC-91).
- Removing the mark or the event removes the photo; sweep() removes any the database no longer knows.
- CC-248: a member's own journal event can have one photo too. Only its member ever sees it, so there's no
  approver: the same Rekognition rule refuses what's never stored, and anything else is shown at once.
"""
import hashlib
import hmac
import io
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from . import keys
from .config import get_settings
from .models import OwnEvent, PhotoCheck, WentPhoto

MAX_BYTES = 3_000_000  # after the browser shrinks it; about 200 KB is usual
UPLOADS_PER_DAY = 10
LINK_MINUTES = 15
CHECK_USD = 0.001  # Rekognition DetectModerationLabels, the first million photos a month
MAX_PIXELS = 12_000_000  # CC-197: width x height we'll decode (a phone camera is about 12 MP)
MAX_SIDE = 1600  # CC-197: the longest side we keep, as the browser already sends
DECODERS = threading.BoundedSemaphore(2)  # CC-197: decodes at once, so parallel uploads can't fill a small server
MAGIC = {"image/jpeg": (b"\xff\xd8\xff",), "image/webp": (b"RIFF",)}
REFUSE_AT = 90.0  # confidence (%) at which these are refused outright
REFUSE = {"Explicit Nudity", "Explicit", "Explicit Sexual Activity", "Sex Toys", "Graphic Violence",
          "Graphic Violence Or Gore", "Hate Symbols", "Nazi Party", "White Supremacy", "Extremist"}
BLUR_AT = 50.0  # any label this sure blurs the photo for approvers


class Refused(Exception):
    """Rekognition found content we never store."""


class BadPhoto(ValueError):
    """CC-197: the bytes aren't a photo we can read, or it's too large."""


class Clash(Exception):
    """CC-197: the mark, the event or the old photo changed while this one was being saved."""


def kind_of(data: bytes, content_type: str) -> str | None:
    """The photo's type, if it's one we take and its bytes agree."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "image/webp" and not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return None
    return ct if ct in MAGIC and data.startswith(MAGIC[ct]) else None


def clean(data: bytes) -> bytes:
    """CC-197: any JPEG or WebP -> a new JPEG: orientation applied, colours in sRGB, no metadata, at most MAX_SIDE.
    Rekognition and storage both get these bytes, never the upload. Raises BadPhoto."""
    from PIL import Image, ImageOps  # Pillow only loads where photos are made, not on every API request

    with DECODERS:
        try:
            im = Image.open(io.BytesIO(data), formats=["JPEG", "WEBP"])
            if im.width * im.height > MAX_PIXELS:  # before load(), which is what takes the memory
                raise BadPhoto("too many pixels")
            im.load()  # an animated WebP gives its first frame
            im = ImageOps.exif_transpose(im)
            profile = im.info.get("icc_profile")
            has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            if profile:  # browsers assume sRGB once the profile is gone, so convert rather than drop it
                from PIL import ImageCms
                try:
                    im = ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                                   ImageCms.createProfile("sRGB"),
                                                   outputMode="RGBA" if has_alpha else "RGB")
                except Exception:  # a broken profile: keep the pixels as they are
                    pass
            if has_alpha:  # JPEG has no transparency: put it on white
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, (255, 255, 255))
                flat.paste(im, mask=im.getchannel("A"))
                im = flat
            else:
                im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85)  # no exif= or icc_profile=, so no metadata is written
            return out.getvalue()
        except BadPhoto:
            raise
        except Exception as err:  # Pillow raises several kinds for unreadable files
            raise BadPhoto(str(err)) from err


# ---- checking (Amazon Rekognition) ----------------------------------------------------------

def _rekognition(data: bytes) -> list[dict]:
    import boto3  # a new client each time: production credentials are short-lived (app/mail.py)
    client = boto3.session.Session().client("rekognition", region_name=get_settings().photo_region)
    return client.detect_moderation_labels(Image={"Bytes": data}, MinConfidence=BLUR_AT)["ModerationLabels"]


def moderation_labels(data: bytes) -> list[dict]:
    """[{"name", "parent", "confidence"}]. Locally there's no Rekognition, so nothing is found."""
    if get_settings().photo_moderation != "rekognition":
        return []
    return [{"name": lab["Name"], "parent": lab.get("ParentName", ""), "confidence": round(float(lab["Confidence"]), 1)}
            for lab in _rekognition(data)]


def judge(labels: list[dict]) -> bool:
    """True to blur for approvers. Raises Refused for content that's never stored."""
    for lab in labels:
        if lab["confidence"] >= REFUSE_AT and (lab["name"] in REFUSE or lab["parent"] in REFUSE):
            raise Refused(lab["name"])
    return any(lab["confidence"] >= BLUR_AT for lab in labels)


# ---- storage --------------------------------------------------------------------------------

class LocalStore:
    def __init__(self, root: str):
        self.root = Path(root)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)

    def keys(self):
        """(key, last modified) for every stored photo."""
        for p in self.root.glob("photos/*"):
            yield f"photos/{p.name}", datetime.fromtimestamp(p.stat().st_mtime, UTC)


class S3Store:
    def __init__(self, bucket: str, region: str):
        self.bucket, self.region = bucket, region

    def _s3(self):
        import boto3
        return boto3.session.Session().client("s3", region_name=self.region)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._s3().put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                              ServerSideEncryption="AES256", CacheControl="private, max-age=600")

    def get(self, key: str) -> bytes:
        return self._s3().get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self._s3().delete_object(Bucket=self.bucket, Key=key)

    def keys(self):
        for page in self._s3().get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="photos/"):
            for obj in page.get("Contents", []):
                yield obj["Key"], obj["LastModified"]


def store():
    s = get_settings()
    return S3Store(s.photo_bucket, s.photo_region) if s.photo_storage == "s3" else LocalStore(s.photo_dir)


# ---- signed links ---------------------------------------------------------------------------

def _sig(photo_id: int, exp: int, kind: bytes = b"went-photo-link:") -> str:
    key = hashlib.sha256(kind + keys.photo_link_secret().encode()).digest()
    return hmac.new(key, f"{photo_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


OWN = b"own-event-photo-link:"  # CC-248: its own key, so a went-photo link can't open an own event's photo


def link(photo: WentPhoto, api_base: str = "") -> str:
    """A link to the photo that works for LINK_MINUTES, for an <img> (which can't send a sign-in)."""
    exp = int(time.time()) + LINK_MINUTES * 60
    return f"{api_base}/photos/{photo.id}?exp={exp}&sig={_sig(photo.id, exp)}"


def link_ok(photo_id: int, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(sig, _sig(photo_id, exp))


def own_link(own: OwnEvent, api_base: str = "") -> str:
    """CC-248: the same kind of link, for an own event's photo."""
    exp = int(time.time()) + LINK_MINUTES * 60
    return f"{api_base}/photos/own/{own.id}?exp={exp}&sig={_sig(own.id, exp, OWN)}"


def own_link_ok(own_id: int, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(sig, _sig(own_id, exp, OWN))


# ---- saving and removing --------------------------------------------------------------------

def uploads_today(db, user_id) -> int:
    since = datetime.now(UTC) - timedelta(days=1)
    return db.scalar(select(func.count()).select_from(PhotoCheck).where(PhotoCheck.user_id == user_id,
                                                                       PhotoCheck.checked_at >= since)) or 0


def _user_lock(db, user_id) -> None:
    """On Postgres, one request at a time per member, so parallel uploads can't both pass the daily limit.
    Held until the next commit or rollback, so never across Rekognition or storage (pattern: emailcaps._lock).
    SQLite (tests) runs one request at a time anyway."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(1843, hashtext(:key))"), {"key": str(user_id)})


def reserve(db, user_id) -> int | None:
    """CC-197: under the member's lock, count today's checks; None at the limit, else record a "checking" one
    and commit (which frees the lock) before Rekognition is called. That committed row is what holds the limit."""
    _user_lock(db, user_id)
    if uploads_today(db, user_id) >= UPLOADS_PER_DAY:
        db.rollback()
        return None
    check = PhotoCheck(user_id=user_id, outcome="checking")
    db.add(check)
    db.flush()
    check_id = check.id
    db.commit()
    return check_id


def _outcome(db, check_id: int, outcome: str) -> None:
    check = db.get(PhotoCheck, check_id)
    if check is not None:
        check.outcome = outcome
        db.commit()


def _checked(db, check_id: int, data: bytes) -> tuple[list[dict], bool]:
    """Rekognition's labels and whether to blur. Raises Refused (counted). If Rekognition itself fails, the
    check is deleted: nothing was billed, so it doesn't use up one of the member's photos."""
    try:
        labels = moderation_labels(data)
        return labels, judge(labels)
    except Refused:
        _outcome(db, check_id, "refused")  # the photo itself is never stored
        raise
    except Exception:
        db.rollback()
        check = db.get(PhotoCheck, check_id)
        if check is not None:
            db.delete(check)
            db.commit()
        raise


def _new_key() -> str:
    return f"photos/{uuid.uuid4().hex}.jpg"  # clean() always makes a JPEG


def _delete_quietly(key: str | None) -> None:
    if key:
        try:
            store().delete(key)
        except Exception:  # noqa: BLE001 - the sweep gets it next time
            pass


def _put(db, check_id: int, key: str, data: bytes) -> None:
    try:
        store().put(key, data, "image/jpeg")
    except Exception:
        db.rollback()
        _delete_quietly(key)  # a put that failed part-way may have left something
        _outcome(db, check_id, "failed")  # Rekognition was billed, so it counts
        raise


def _failed(db, check_id: int, key: str, err: Exception):
    """Any failure after the file was put: no file, no row, a counted check. A clash becomes Clash."""
    db.rollback()
    _delete_quietly(key)
    _outcome(db, check_id, "failed")
    if isinstance(err, (IntegrityError, InvalidRequestError)):  # InvalidRequestError: the own event is gone
        raise Clash() from err
    raise err


def save(db, user_id, event_id: int, check_id: int, data: bytes, content_type: str) -> WentPhoto:
    """Check, store and record a member's photo for an event they've been to. data is clean()'s output and
    check_id is reserve()'s. Raises Refused, or Clash if the mark changed meanwhile. Commits."""
    labels, blurred = _checked(db, check_id, data)
    key = _new_key()
    _put(db, check_id, key, data)
    try:
        _user_lock(db, user_id)
        old = db.scalar(select(WentPhoto).where(WentPhoto.user_id == user_id, WentPhoto.event_id == event_id))
        old_key = old.key if old is not None else None
        if old is not None:
            db.delete(old)
            db.flush()
        photo = WentPhoto(user_id=user_id, event_id=event_id, key=key, content_type=content_type, size=len(data),
                          status="waiting", blurred=blurred, labels=labels)
        db.add(photo)
        check = db.get(PhotoCheck, check_id)
        if check is not None:
            check.outcome = "stored"
        db.commit()
    except Exception as err:
        _failed(db, check_id, key, err)
    _delete_quietly(old_key)  # only once the new photo is committed
    return photo


def save_own(db, own: OwnEvent, check_id: int, data: bytes, content_type: str) -> None:
    """CC-248: check and store the photo for a member's own event, replacing any it had. Raises Refused, or Clash.
    No approver, so blurring doesn't apply. Commits."""
    user_id = own.user_id
    _checked(db, check_id, data)
    key = _new_key()
    _put(db, check_id, key, data)
    try:
        _user_lock(db, user_id)
        db.refresh(own)  # a second upload may have replaced the photo while this one was checked
        old_key = own.photo_key
        own.photo_key, own.photo_type, own.photo_at = key, content_type, datetime.now(UTC)
        check = db.get(PhotoCheck, check_id)
        if check is not None:
            check.outcome = "stored"
        db.commit()
    except Exception as err:
        _failed(db, check_id, key, err)
    _delete_quietly(old_key)


def remove_own(own: OwnEvent) -> None:
    """CC-248: delete an own event's photo from storage, if it has one. Doesn't commit."""
    if own.photo_key:
        try:
            store().delete(own.photo_key)
        except Exception:  # noqa: BLE001 - the sweep gets it next time
            pass
    own.photo_key, own.photo_type, own.photo_at = None, "", None


def remove(db, photos) -> None:
    """Delete these photos from storage and the database. Doesn't commit."""
    for photo in photos:
        try:
            store().delete(photo.key)
        except Exception:  # noqa: BLE001 - the sweep gets it next time
            pass
        db.delete(photo)


def sweep(db, older_than_minutes: int = 60) -> int:
    """Remove stored photos the database doesn't know (their mark, event or account is gone). Returns how many."""
    known = set(db.scalars(select(WentPhoto.key))) | set(db.scalars(select(OwnEvent.photo_key)))  # CC-248
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)  # not one being saved right now
    gone = [key for key, at in store().keys() if key not in known and at < cutoff]
    for key in gone:
        store().delete(key)
    return len(gone)


def checks_this_month(db, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count()).select_from(PhotoCheck).where(PhotoCheck.checked_at >= start)) or 0
