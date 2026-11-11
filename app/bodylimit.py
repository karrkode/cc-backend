"""CC-190: refuse oversized requests before they're read into memory.

Caddy stops anything over 4 MB (CC-183). Here the limit is 1 MB, far above any real JSON request. The one route
that needs more is the photo upload, which streams its body and keeps its own limit (photos.MAX_BYTES).
"""
import re

from fastapi import HTTPException, status
from starlette.responses import JSONResponse

MAX_BYTES = 1_000_000
PHOTO_UPLOAD = re.compile(r"(?:/me/(?:attended|own-events)/\d+/photo|/events/\d+/image)")  # CC-265: admin image upload too
TOO_BIG = "That's too much to send in one go."
CLOSE = {"Connection": "close"}  # the rest of the body isn't wanted: don't wait for it


def _declared(scope) -> int:
    """The Content-Length, or 0 if there's none we can read. The count of what really arrives still applies."""
    try:
        return int(dict(scope["headers"]).get(b"content-length", b"0"))
    except ValueError:
        return 0


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (scope["method"] == "PUT" and PHOTO_UPLOAD.fullmatch(scope["path"])):
            await self.app(scope, receive, send)
            return
        if _declared(scope) > MAX_BYTES:
            response = JSONResponse({"detail": TOO_BIG}, status.HTTP_413_CONTENT_TOO_LARGE, headers=CLOSE)
            await response(scope, receive, send)
            return
        seen = 0

        async def counted():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > MAX_BYTES:  # a body with no Content-Length, or more than it said
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, TOO_BIG, headers=CLOSE)
            return message
        await self.app(scope, counted, send)
