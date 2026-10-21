"""Photos with "I've been" (CC-76), as agreed with the product owner.

- Optional: "I've been" never needs one. One photo per member per event; a new one replaces it.
- The browser shrinks it (longest side about 1600px) and re-encodes it as JPEG, which drops location and
  camera details. The API takes JPEG or WebP only, up to MAX_BYTES, and up to UPLOADS_PER_DAY a member.
- Amazon Rekognition (DetectModerationLabels, $0.001 a photo) checks it before it's stored. Explicit,
  graphically violent or hateful content at high confidence is refused and never stored. Anything else
  it finds blurs the photo for approvers (CC-91), with its labels. Every photo waits for an approver.
- Stored privately: an S3 bucket in production (encrypted, no public access), a folder locally. Shown
  only through short-lived links signed by the API, to the member and to admins (approvers: CC-91).
- Removing the mark or the event removes the photo; sweep() removes any the database no longer knows.
"""
import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from .config import get_settings
from .models import PhotoCheck, WentPhoto

MAX_BYTES = 3_000_000  # after the browser shrinks it; about 200 KB is usual
UPLOADS_PER_DAY = 10
LINK_MINUTES = 15
CHECK_USD = 0.001  # Rekognition DetectModerationLabels, the first million photos a month
MAGIC = {"image/jpeg": (b"\xff\xd8\xff",), "image/webp": (b"RIFF",)}
REFUSE_AT = 90.0  # confidence (%) at which these are refused outright
REFUSE = {"Explicit Nudity", "Explicit", "Explicit Sexual Activity", "Sex Toys", "Graphic Violence",
          "Graphic Violence Or Gore", "Hate Symbols", "Nazi Party", "White Supremacy", "Extremist"}
BLUR_AT = 50.0  # any label this sure blurs the photo for approvers


class Refused(Exception):
    """Rekognition found content we never store."""


def kind_of(data: bytes, content_type: str) -> str | None:
    """The photo's type, if it's one we take and its bytes agree."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "image/webp" and not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return None
    return ct if ct in MAGIC and data.startswith(MAGIC[ct]) else None


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

def _sig(photo_id: int, exp: int) -> str:
    key = hashlib.sha256(b"went-photo-link:" + get_settings().jwt_secret.encode()).digest()
    return hmac.new(key, f"{photo_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def link(photo: WentPhoto, api_base: str = "") -> str:
    """A link to the photo that works for LINK_MINUTES, for an <img> (which can't send a sign-in)."""
    exp = int(time.time()) + LINK_MINUTES * 60
    return f"{api_base}/photos/{photo.id}?exp={exp}&sig={_sig(photo.id, exp)}"


def link_ok(photo_id: int, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(sig, _sig(photo_id, exp))


# ---- saving and removing --------------------------------------------------------------------

def uploads_today(db, user_id) -> int:
    since = datetime.now(UTC) - timedelta(days=1)
    return db.scalar(select(func.count()).select_from(PhotoCheck).where(PhotoCheck.user_id == user_id,
                                                                       PhotoCheck.checked_at >= since)) or 0


def save(db, user_id, event_id: int, data: bytes, content_type: str) -> WentPhoto:
    """Check, store and record a member's photo for an event they've been to. Raises Refused. Doesn't commit."""
    labels = moderation_labels(data)
    check = PhotoCheck(user_id=user_id, outcome="stored")  # every upload counts toward the daily limit and the cost
    db.add(check)
    try:
        blurred = judge(labels)
    except Refused:
        check.outcome = "refused"  # the photo itself is never stored
        raise
    key = f"photos/{uuid.uuid4().hex}.{'webp' if content_type == 'image/webp' else 'jpg'}"
    store().put(key, data, content_type)
    old = db.scalar(select(WentPhoto).where(WentPhoto.user_id == user_id, WentPhoto.event_id == event_id))
    if old is not None:
        store().delete(old.key)
        db.delete(old)
        db.flush()
    photo = WentPhoto(user_id=user_id, event_id=event_id, key=key, content_type=content_type, size=len(data),
                      status="waiting", blurred=blurred, labels=labels)
    db.add(photo)
    return photo


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
    known = set(db.scalars(select(WentPhoto.key)))
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)  # not one being saved right now
    gone = [key for key, at in store().keys() if key not in known and at < cutoff]
    for key in gone:
        store().delete(key)
    return len(gone)


def checks_this_month(db, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count()).select_from(PhotoCheck).where(PhotoCheck.checked_at >= start)) or 0
