"""The site's own emails: confirming an address and resetting a password.

How an email goes out depends on settings.email_mode:
- ses: Amazon SES (production). The SES identity lives in us-east-1 (infra/aws/ses.tf).
- log: the whole email, link included, is written to the API log instead (local development,
  so nobody is ever emailed by accident; copy the link from `docker compose logs api`).
- memory: kept in OUTBOX (tests).
- none: skipped, with a one-line note in the log (and never the link).

Sending never raises: a failed email is logged and the request that triggered it still succeeds.
Emails go out after the response (FastAPI background tasks), so asking for a password reset
takes as long whether or not the address has an account.

Links carry the token in the URL fragment (#verify=..., #reset=...), which browsers never send
to a server, so tokens don't end up in CloudFront or API logs.
"""
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import EmailToken, EmailTokenPurpose, User
from .security import hash_refresh_token as hash_token  # the same SHA-256 hex digest

log = logging.getLogger("uvicorn.error")  # uvicorn already prints this logger at INFO


@dataclass
class Email:
    to: str
    subject: str
    text: str


OUTBOX: list[Email] = []  # the "memory" mode's sent mail, for tests


def _ses():
    """A new client per email. In production the credentials are short-lived and refreshed in a
    file by the host (infra/aws/server/mail-creds.sh); a new client reads the current ones."""
    import boto3  # only production needs it loaded
    return boto3.session.Session().client("sesv2", region_name=get_settings().ses_region)


def send(email: Email) -> None:
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
                                    "Body": {"Text": {"Data": email.text, "Charset": "UTF-8"}}}},
            )
        else:
            log.warning("Email to %s skipped: no email backend is set up (%r)", email.to, email.subject)
    except Exception:
        log.exception("Email to %s failed (%r)", email.to, email.subject)


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


# ---- the emails ------------------------------------------------------------------

def _greeting(user: User) -> str:
    return f"Hi {user.display_name.strip()}," if user.display_name.strip() else "Hi,"


def verify_email(user: User, raw: str) -> Email:
    s = get_settings()
    return Email(user.email, "Confirm your email for Culture Crew", f"""{_greeting(user)}

Please confirm this is your email address for Culture Crew by opening this link:

{s.site_url}/#verify={raw}

The link works once and expires in {s.verify_link_hours} hours. If you didn't create a Culture Crew
account, you can ignore this email.

Culture Crew
{s.site_url}
""")


def reset_password(user: User, raw: str) -> Email:
    s = get_settings()
    return Email(user.email, "Reset your Culture Crew password", f"""{_greeting(user)}

Someone (hopefully you) asked to reset the password for your Culture Crew account.
To choose a new one, open this link:

{s.site_url}/#reset={raw}

The link works once and expires in {s.reset_link_minutes} minutes. Resetting your password signs
you out everywhere. If you didn't ask for this, ignore this email; your password stays the same.

Culture Crew
{s.site_url}
""")
