"""CC-204: the nightly clean-up of sign-in sessions and emailed links (scripts/token_cleanup.py)."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_
from sqlalchemy.orm import Session

from .config import get_settings
from .models import EmailToken, EmailTokenPurpose, RefreshToken


def clean_tokens(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Delete what can no longer be used. Revoked sessions stay until they expire: a replayed old token is
    how a stolen one is caught (the refresh ends every session), so deleting them sooner would switch that off.
    Used reset links go at once; used confirm links stay until they expire, so opening one twice can still say "already confirmed"."""
    now = now or datetime.now(UTC)
    too_old = now - timedelta(days=get_settings().max_session_days)
    sessions = db.execute(delete(RefreshToken).where(
        or_(RefreshToken.expires_at <= now, RefreshToken.signed_in_at <= too_old))).rowcount
    links = db.execute(delete(EmailToken).where(
        or_(EmailToken.expires_at <= now,
            and_(EmailToken.used_at.is_not(None), EmailToken.purpose == EmailTokenPurpose.reset_password)))).rowcount
    db.commit()
    return {"sessions": sessions, "email_links": links}
