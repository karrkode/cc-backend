"""Showing "I've been" photos (CC-76). An <img> can't send a sign-in, so the API hands out links signed
for 15 minutes (app/photos.py link) to the people allowed to see a photo: its member, and admins (approvers
from CC-91). The photo is read from private storage and sent with caching kept private."""
from fastapi import APIRouter, HTTPException, Response, status

from .. import photos
from ..deps import DB
from ..models import OwnEvent, WentPhoto

router = APIRouter(prefix="/photos", tags=["photos"])


@router.get("/{photo_id}")
def show_photo(photo_id: int, exp: int, sig: str, db: DB):
    photo = db.get(WentPhoto, photo_id)
    if photo is None or not photos.link_ok(photo_id, exp, sig):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found")  # an expired link looks the same as none
    return _send(photo.key, photo.content_type)


@router.get("/own/{own_id}")
def show_own_photo(own_id: int, exp: int, sig: str, db: DB):
    """CC-248: the photo of a member's own journal event, for its member (the only one given a link)."""
    own = db.get(OwnEvent, own_id)
    if own is None or not own.photo_key or not photos.own_link_ok(own_id, exp, sig):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found")
    return _send(own.photo_key, own.photo_type)


def _send(key: str, content_type: str) -> Response:
    try:
        data = photos.store().get(key)
    except Exception:  # noqa: BLE001 - gone from storage
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found") from None
    return Response(data, media_type=content_type, headers={
        "Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'", "Cross-Origin-Resource-Policy": "cross-origin",
        "Referrer-Policy": "no-referrer"})
