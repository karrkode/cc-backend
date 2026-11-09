"""CC-184: how many emails one address gets, and one account causes, whatever the route. Every limit is here.

Each email a member can cause (confirm links, password resets, crew invites, change notices) is recorded in sent_emails before it's
queued. Cancelling, declining or expiring an invite doesn't touch that record, so it can't be used to send again.
Over a cap the route answers exactly as usual and sends nothing, so the answer says nothing about the address.
The weekly recap goes to our own inbox from a scheduled job and isn't counted.
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .models import SentEmail

# kinds of email
VERIFY = "verify_email"
RESET = "reset_password"
INVITE = "crew_invite"
NOTICE = "account_notice"  # CC-203: your password or email changed

# one address, every kind together
PER_ADDRESS_PER_HOUR = 3
PER_ADDRESS_PER_DAY = 10
INVITES_TO_ADDRESS_PER_DAY = 3  # CC-83: crew invites from all crews together
# one account
RESEND_GAP = timedelta(minutes=1)  # "send it again" (confirm link, password reset): once a minute
INVITES_PER_ACCOUNT_PER_HOUR = 10  # set by the product owner
INVITES_PER_ACCOUNT_PER_DAY = 20

HOUR, DAY = timedelta(hours=1), timedelta(days=1)


def _lock(db: Session, space: int, key: str) -> None:
    """On Postgres, one request at a time per address (or account), so parallel requests can't both pass a cap.
    Held until the caller commits. SQLite (tests) runs one request at a time anyway."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:space, hashtext(:key))"), {"space": space, "key": key})


def may_send(db: Session, to: str, kind: str, account_id=None, resend: bool = False) -> bool:
    """True, and the email recorded, if it's within every cap; False means don't send it. Doesn't commit."""
    to = to.strip().lower()
    now = datetime.now(UTC)

    def sent(within: timedelta, *where) -> int:
        return db.scalar(select(func.count()).select_from(SentEmail).where(SentEmail.sent_at > now - within, *where)) or 0

    _lock(db, 1841, to)
    if sent(HOUR, SentEmail.email == to) >= PER_ADDRESS_PER_HOUR or sent(DAY, SentEmail.email == to) >= PER_ADDRESS_PER_DAY:
        return False
    if kind == INVITE and sent(DAY, SentEmail.email == to, SentEmail.kind == INVITE) >= INVITES_TO_ADDRESS_PER_DAY:
        return False
    if account_id is not None:
        _lock(db, 1842, str(account_id))  # always after the address lock, so two requests can't wait on each other
        mine = (SentEmail.account_id == account_id, SentEmail.kind == kind)
        if resend and sent(RESEND_GAP, *mine):
            return False
        if kind == INVITE and (sent(HOUR, *mine) >= INVITES_PER_ACCOUNT_PER_HOUR
                               or sent(DAY, *mine) >= INVITES_PER_ACCOUNT_PER_DAY):
            return False
    db.add(SentEmail(email=to, kind=kind, account_id=account_id, sent_at=now))
    return True
