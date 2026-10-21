"""Showing "I've been" photos (CC-76). An <img> can't send a sign-in, so the API hands out links signed
for 15 minutes (app/photos.py link) to the people allowed to see a photo: its member, and admins (approvers
from CC-91). The photo is read from private storage and sent with caching kept private."""
from fastapi import APIRouter, HTTPException, Response, status

from .. import photos
from ..deps import DB
from ..models import WentPhoto

router = APIRouter(prefix="/photos", tags=["photos"])


@router.get("/{photo_id}")
def show_photo(photo_id: int, exp: int, sig: str, db: DB):
    photo = db.get(WentPhoto, photo_id)
    if photo is None or not photos.link_ok(photo_id, exp, sig):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found")  # an expired link looks the same as none
    try:
        data = photos.store().get(photo.key)
    except Exception:  # noqa: BLE001 - gone from storage
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found") from None
    return Response(data, media_type=photo.content_type, headers={
        "Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'", "Cross-Origin-Resource-Policy": "cross-origin",
        "Referrer-Policy": "no-referrer"})
