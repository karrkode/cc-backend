"""Event photos (CC-250), as agreed with the product owner.

- One photo per event, optional: an event without one shows its category water.
- Every photo is normalised the same way, whatever it came in as (JPEG, PNG, WebP, AVIF, GIF): turned upright,
  converted to sRGB, at most MAX_SIDE pixels on the long side, and saved as WebP at QUALITY. Metadata (location,
  camera, colour profiles) is dropped. The original is kept privately beside it, so photos can be re-made later.
- Public: the event-images bucket (infra/aws/event_images.tf) is served by the site's CloudFront at /event-images/*.
  Keys carry a hash of the bytes, so a new photo gets a new address and browsers can cache each one for a year.
- Locally the photos go in a folder (event_image_dir) that docker-compose.yml serves on :4173 at the same path.
- The first photos came from a one-off search (scripts.event_images). CC-246: event sourcing stores new ones
  (app/discovery/pictures.py) through store_photo(), which fills image_key and image_source_url the same way.
"""
import hashlib
import io
from pathlib import Path

from .config import get_settings

MAX_SIDE = 1200  # the widest catalog card is about 450 CSS pixels; this covers 2x screens and the poster
QUALITY = 80
MAX_INPUT_BYTES = 25_000_000  # the largest of the first batch is under 6 MB
# CC-246: a picture is read before its pixels are, so a small file that unpacks into a huge image is refused (CC-197)
MAX_PIXELS = 50_000_000
PREFIX = "event-images"  # the CloudFront path, and the bucket's served prefix
ORIGINALS = "originals"  # never routed by CloudFront


class BadImage(ValueError):
    """The bytes aren't an image Pillow can read."""


def normalise(data: bytes) -> bytes:
    """Any supported image -> the WebP we serve."""
    from PIL import Image, ImageOps  # Pillow only loads where photos are made, not on every API request

    if len(data) > MAX_INPUT_BYTES:
        raise BadImage("too large")
    try:
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_PIXELS:
            raise BadImage("too many pixels")
        im.load()
    except BadImage:
        raise
    except Exception as err:  # Pillow raises several kinds for unreadable files
        raise BadImage(str(err)) from err
    im = ImageOps.exif_transpose(im)
    profile = im.info.get("icc_profile")
    if profile:  # CC-250: browsers assume sRGB once the profile is gone, so convert rather than drop it
        from PIL import ImageCms
        try:
            im = ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                           ImageCms.createProfile("sRGB"),
                                           outputMode="RGBA" if im.mode in ("RGBA", "LA", "PA") else "RGB")
        except Exception:  # a broken profile: keep the pixels as they are
            pass
    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info else "RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=QUALITY, method=6)  # no exif= or icc_profile=, so no metadata is written
    return out.getvalue()


def key_for(event_id: int, webp: bytes) -> str:
    return f"{PREFIX}/{event_id}/{hashlib.sha256(webp).hexdigest()[:16]}.webp"


def original_key_for(event_id: int, data: bytes, ext: str) -> str:
    ext = ext.lower().lstrip(".") or "bin"
    return f"{ORIGINALS}/{event_id}/{hashlib.sha256(data).hexdigest()[:16]}.{ext}"


def url_for(key: str | None) -> str | None:
    if not key:
        return None
    return f"{get_settings().site_url.rstrip('/')}/{key}"


class LocalStore:
    def __init__(self, root: str):
        self.root = Path(root)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def exists(self, key: str) -> bool:
        return (self.root / key).is_file()


class S3Store:
    def __init__(self, bucket: str, region: str):
        self.bucket, self.region = bucket, region

    def _s3(self):
        import boto3  # a new client each time: production credentials are short-lived (app/mail.py)
        return boto3.session.Session().client("s3", region_name=self.region)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        # Served keys never change (their name is their hash), so browsers and CloudFront keep them a year.
        cache = "public, max-age=31536000, immutable" if key.startswith(PREFIX + "/") else "private, max-age=0"
        self._s3().put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                              ServerSideEncryption="AES256", CacheControl=cache)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self._s3().head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False


def store():
    s = get_settings()
    return S3Store(s.event_image_bucket, s.event_image_region) if s.event_image_storage == "s3" \
        else LocalStore(s.event_image_dir)


def save(event_id: int, data: bytes, ext: str, to=None, webp: bytes | None = None) -> str:
    """Store the original and its WebP (made here unless given); returns the served key. Uploading the same bytes
    again is a no-op."""
    to = to or store()
    webp = webp or normalise(data)
    key = key_for(event_id, webp)
    if not to.exists(key):
        to.put(original_key_for(event_id, data, ext), data, "application/octet-stream")
        to.put(key, webp, "image/webp")
    return key


def store_photo(event, data: bytes, ext: str, source_url: str = "", to=None, webp: bytes | None = None) -> str:
    """Give an event its photo (the caller commits). For sourcing (CC-246), and for the one-off import's local run."""
    event.image_key = save(event.id, data, ext, to, webp)
    event.image_source_url = source_url or ""
    return event.image_key
