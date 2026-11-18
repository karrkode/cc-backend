"""CC-209: members suggest a venue's calendar link; admins accept it (which adds a discovery source) or decline it.

- Signed in, with a confirmed email, a few a day per member (counted from what was saved, so it holds across restarts
  and servers).
- The link is checked like any outgoing fetch: http(s) on the usual ports at public addresses (CC-189). The shared fetch
  checks again when a source is read; this is the early answer, and runs again when an admin accepts.
- Nothing is fetched when a suggestion is made, so a member can't use this to make the server read pages.
- A link a source already reads, or a pending suggestion already has, is refused with the same message for everyone,
  so it says nothing about who suggested it.
- No public credit: only admins see who suggested it.
"""
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException, status
from sqlalchemy import func, select

from .clock import utcnow
from .discovery.fetch import PrivateAddress, check_public
from .discovery.venuesources import _parts
from .models import DiscoverySource, VenueSuggestion

PER_DAY = 5
ALREADY = "We already have that one, or it's waiting for review. Thank you!"


def clean_url(url: str) -> str:
    """The link as stored: http(s) at a public host, without a fragment. HTTPException 422 with a message otherwise."""
    url = url.strip()
    try:
        check_public(url)
        parts = urlsplit(url)
    except (PrivateAddress, ValueError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"We can't use that link: {e}.") from e
    if parts.username or parts.password:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "We can't use that link: it has a sign-in in it.")
    return parts._replace(fragment="").geturl()


def read_already(db, url: str) -> bool:
    """Whether a source already reads this page (same site, same path), or a suggestion for it is waiting."""
    host, path = _parts(url)
    for source in db.scalars(select(DiscoverySource)):
        if any(_parts(p) == (host, path) for p in (source.pages or [])) or _parts(source.added_from) == (host, path):
            return True
    return any(_parts(u) == (host, path) for u in db.scalars(select(VenueSuggestion.url).where(VenueSuggestion.status == "pending")))


def over_limit(db, user) -> bool:
    since = utcnow() - timedelta(days=1)
    n = db.scalar(select(func.count()).select_from(VenueSuggestion).where(VenueSuggestion.user_id == user.id, VenueSuggestion.created_at >= since))
    return (n or 0) >= PER_DAY
