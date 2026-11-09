"""The site's own emails: confirming an address, resetting a password, change notices, crew invites, and the weekly
discovery recap.

How an email goes out depends on settings.email_mode:
- ses: Amazon SES (production). The SES identity lives in us-east-1 (infra/aws/ses.tf).
- log: the whole email, link included, is written to the API log instead (local development,
  so nobody is ever emailed by accident; copy the link from `docker compose logs api`).
- memory: kept in OUTBOX (tests).
- none: skipped, with a one-line note in the log (and never the link).

Sending never raises (unless strict): a failed email is logged and the request that triggered it still
succeeds. The scheduled recap sends strictly, so a failure fails the run and alerts.
Emails go out after the response (FastAPI background tasks), so asking for a password reset
takes as long whether or not the address has an account.

Links carry the token in the URL fragment (#verify=..., #reset=...), which browsers never send
to a server, so tokens don't end up in CloudFront or API logs.
"""
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import EmailToken, EmailTokenPurpose, User
from .security import hash_refresh_token as hash_token  # the same SHA-256 hex digest

log = logging.getLogger("uvicorn.error")  # uvicorn already prints this logger at INFO
CONTACT = "info@chiculturecrew.com"


@dataclass
class Email:
    to: str
    subject: str
    text: str
    html: str = ""  # optional; sent alongside the text, which stays the fallback


OUTBOX: list[Email] = []  # the "memory" mode's sent mail, for tests


def _ses():
    """A new client per email. In production the credentials are short-lived and refreshed in a
    file by the host (infra/aws/server/mail-creds.sh); a new client reads the current ones."""
    import boto3  # only production needs it loaded
    return boto3.session.Session().client("sesv2", region_name=get_settings().ses_region)


def send(email: Email, strict: bool = False) -> None:
    s = get_settings()
    mode = s.email_mode
    try:
        if mode == "memory":
            OUTBOX.append(email)
        elif mode == "log":
            log.info("Email to %s (not sent: EMAIL_BACKEND=log)\nSubject: %s\n\n%s", email.to, email.subject, email.text)
        elif mode == "ses":
            _ses().send_email(
                FromEmailAddress=s.mail_from,
                Destination={"ToAddresses": [email.to]},
                ReplyToAddresses=[s.mail_reply_to] if s.mail_reply_to else [],
                Content={"Simple": {"Subject": {"Data": email.subject, "Charset": "UTF-8"},
                                    "Body": {"Text": {"Data": email.text, "Charset": "UTF-8"},
                                             **({"Html": {"Data": email.html, "Charset": "UTF-8"}} if email.html else {})}}},
            )
        else:
            log.warning("Email to %s skipped: no email backend is set up (%r)", email.to, email.subject)
    except Exception:
        log.exception("Email to %s failed (%r)", email.to, email.subject)
        if strict:
            raise


# ---- single-use links ------------------------------------------------------------

def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite hands back naive datetimes


def issue_token(db: Session, user: User, purpose: EmailTokenPurpose, lifetime: timedelta) -> str:
    """A new link for this user and purpose. Earlier unused ones stop working. Doesn't commit."""
    db.execute(delete(EmailToken).where(EmailToken.user_id == user.id, EmailToken.purpose == purpose,
                                        EmailToken.used_at.is_(None)))
    raw = secrets.token_urlsafe(32)
    db.add(EmailToken(user_id=user.id, purpose=purpose, token_hash=hash_token(raw), email=user.email,
                      expires_at=datetime.now(UTC) + lifetime))
    return raw


def redeem_token(db: Session, raw: str, purpose: EmailTokenPurpose) -> User | None:
    """The user a link belongs to, marking it used; None if it's unknown, used, expired, for
    something else, or was sent to an address the account no longer has. Doesn't commit."""
    token = db.scalar(select(EmailToken).where(EmailToken.token_hash == hash_token(raw),
                                               EmailToken.purpose == purpose).with_for_update())
    if token is None or token.used_at is not None or _utc(token.expires_at) <= datetime.now(UTC):
        return None
    user = db.get(User, token.user_id)
    if user is None or user.email != token.email:
        return None
    token.used_at = datetime.now(UTC)
    return user


def already_confirmed_by(db: Session, raw: str) -> User | None:
    """The user, if this confirm link was already used and the address it was sent to is still
    theirs and confirmed. Mail scanners often open links before the person does; they then
    see "already confirmed" instead of an error."""
    token = db.scalar(select(EmailToken).where(EmailToken.token_hash == hash_token(raw),
                                               EmailToken.purpose == EmailTokenPurpose.verify_email))
    if token is None or token.used_at is None:
        return None
    user = db.get(User, token.user_id)
    return user if user is not None and user.email == token.email and user.email_verified else None


# ---- the emails ------------------------------------------------------------------

# CC-185: opening lines are only ever our own wording. Text a member chose (a display name, a crew name)
# comes lower down, labelled, on one line, without anything that reads as a link. The one exception is
# the inviter's cleaned, capped name in the invite subject, so a friend's invite is recognisable.

def verify_email(user: User, raw: str) -> Email:
    s = get_settings()
    return Email(user.email, "Confirm your email for Culture Crew", f"""Hi,

Please confirm this is your email address for Culture Crew by opening this link:

{s.site_url}/#verify={raw}

The link works once and expires in {s.verify_link_hours} hours. If you didn't create a Culture Crew
account, you can ignore this email.

Culture Crew
{s.site_url}
""")


def reset_password(user: User, raw: str) -> Email:
    s = get_settings()
    return Email(user.email, "Reset your Culture Crew password", f"""Hi,

Someone (hopefully you) asked to reset the password for your Culture Crew account.
To choose a new one, open this link:

{s.site_url}/#reset={raw}

The link works once and expires in {s.reset_link_minutes} minutes. Resetting your password signs
you out everywhere. If you didn't ask for this, ignore this email; your password stays the same.

Culture Crew
{s.site_url}
""")


def password_changed(user: User, how: str) -> Email:
    """CC-203: the account's password was changed (from the account page) or reset (from an emailed link)."""
    s = get_settings()
    return Email(user.email, "Your Culture Crew password was changed", f"""Hi,

The password for your Culture Crew account was just {how}. Any other devices that were signed in
have been signed out.

If this was you, there's nothing more to do. If it wasn't, reset your password now with
"Forgot password?" on the sign-in page, and write to us at {CONTACT}.

{s.site_url}/#account

Culture Crew
{s.site_url}
""")


def email_changed(old_email: str) -> Email:
    """CC-203: sent to the address an account used to have. It doesn't name the new address, which
    may not belong to whoever reads this."""
    s = get_settings()
    return Email(old_email, "The email for your Culture Crew account was changed", f"""Hi,

The email address for the Culture Crew account that used this address has just been changed,
so we won't send account emails here any more.

If this was you, there's nothing more to do. If it wasn't, write to us at {CONTACT} and we'll
help you get the account back.

Culture Crew
{s.site_url}
""")


# a scheme, "www.", an @, or a dot between letters ("evil.example", "x.co/y"): the whole word goes
_LINKISH = re.compile(r"\S*(?:://|www\.|@|[^\W\d_]\.[^\W\d_]{2,})\S*", re.IGNORECASE)
NAME_CAP = 40


def plain_name(text: str, fallback: str, cap: int = NAME_CAP) -> str:
    """A member's name as an email may show it: one line, no control characters, nothing link-like,
    at most `cap` characters. Names saved before CC-185 may break those rules; they're cleaned here."""
    text = "".join(c if c.isprintable() else " " for c in text or "")
    text = " ".join(_LINKISH.sub(" ", text).split())
    if len(text) > cap:
        text = text[:cap - 1].rstrip() + "…"
    return text or fallback


def crew_invite(to: str, inviter: str, crew: str, days: int) -> Email:
    """CC-83: someone was invited to a crew by email. Says who and which crew, never who else is in it,
    and reads the same whether or not the address has an account."""
    s = get_settings()
    inviter, crew = plain_name(inviter, "A member"), plain_name(crew, "A crew", cap=60)
    confirm = " (a new account confirms its email address first)" if s.require_verified_email else ""
    return Email(to, f"{inviter} invited you to a crew on Culture Crew", f"""Hi,

You've been invited to join a crew on Culture Crew, to find and plan Chicago culture outings together.

Invited by: {inviter}
Crew: {crew}
(Members choose these names themselves.)

To join, sign in or create an account with this email address{confirm}, then accept
the invite under "Waiting for you":

{s.site_url}/#account

The invite waits for {days} days. If you don't know who invited you, you can ignore this email.

Culture Crew
{s.site_url}
""")
