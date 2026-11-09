from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError

from .. import emailcaps, mail
from ..config import get_settings
from ..deps import DB, CurrentUser
from ..models import EmailTokenPurpose, RefreshToken, Role, User
from ..ratelimit import limit_auth_attempts
from ..schemas import (ForgotPasswordIn, LoginIn, MessageOut, PasswordChangeIn, ProfilePatch, RegisterIn,
                       ResetPasswordIn, TokenIn, TokenOut, UserOut, RESERVED_NAMES, name_key)
from ..security import (create_access_token, hash_password, hash_refresh_token, new_refresh_token,
                        verify_password)
from ..stats import record_active, today_in_chicago
from ..traffic import record_join_signup, source_of

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE = "cc_refresh"
COOKIE_PATH = "/auth"  # the browser only sends it to the auth endpoints
REUSE_GRACE_SECONDS = 30


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite hands back naive datetimes


def _check_name_free(db: DB, name: str, user: User | None = None) -> None:
    """CC-185: members can't call themselves by the site's own names, or by the name of an admin or of
    an account whose picks the site shows (CC-61). Those accounts themselves can use any name."""
    key = name_key(name)
    if not key or (user is not None and (user.role == Role.admin or user.dolphins)):
        return
    admins = select(User.display_name).where(or_(User.role == Role.admin, User.dolphins.is_(True)))
    if user is not None:
        admins = admins.where(User.id != user.id)
    if key in RESERVED_NAMES or any(name_key(a) == key for a in db.scalars(admins)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That name is reserved. Choose another.")


def _issue(db: DB, user: User, response: Response, signed_in_at: datetime | None = None) -> TokenOut:
    """Make a new access token, and a new refresh token in the cookie.

    signed_in_at is when this session began with a password: now for a sign-in, and the old token's
    for a refresh (CC-203). The refresh token never outlives max_session_days from then."""
    s = get_settings()
    now = datetime.now(UTC)
    signed_in_at = signed_in_at or now
    expires_at = min(now + timedelta(days=s.refresh_token_days), signed_in_at + timedelta(days=s.max_session_days))
    raw = new_refresh_token()
    db.add(RefreshToken(user_id=user.id, token_hash=hash_refresh_token(raw), expires_at=expires_at,
                        signed_in_at=signed_in_at))
    record_active(db, user)  # CC-67: every visit by a signed-in member starts or refreshes a session
    db.commit()
    response.set_cookie(COOKIE, raw, max_age=int((expires_at - now).total_seconds()), path=COOKIE_PATH,
                        domain=s.cookie_domain, secure=s.cookie_secure, httponly=True, samesite="lax")
    access, expires_in = create_access_token(user.id, user.role.value, user.session_version)
    return TokenOut(access_token=access, expires_in=expires_in, user=UserOut.model_validate(user))


def _end_all_sessions(db: DB, user: User):
    """Sign the account out everywhere: no refresh token survives, and (CC-203) raising the session
    version ends the access tokens already handed out too. Doesn't commit."""
    db.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
    user.session_version = (user.session_version or 0) + 1


def _verification_email(db: DB, user: User) -> mail.Email:
    """Make a confirm-your-email link. The caller commits, and only then queues the email,
    so a request that fails never sends one."""
    raw = mail.issue_token(db, user, EmailTokenPurpose.verify_email,
                           timedelta(hours=get_settings().verify_link_hours))
    return mail.verify_email(user, raw)


def _clear_cookie(response: Response):
    s = get_settings()
    response.delete_cookie(COOKIE, path=COOKIE_PATH, domain=s.cookie_domain, secure=s.cookie_secure,
                           httponly=True, samesite="lax")


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(limit_auth_attempts)])
def register(body: RegisterIn, db: DB, response: Response, tasks: BackgroundTasks):
    """Create an account and sign in. A link to confirm the address is emailed after the response."""
    _check_name_free(db, body.display_name)
    user = User(email=body.email.lower(), password_hash=hash_password(body.password),
                display_name=body.display_name.strip())
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists")
    if body.join_source is not None:  # CC-121: a sign-up from the join page, counted by source with the account
        j = body.join_source
        record_join_signup(db, today_in_chicago(), source_of(j.utm_source, j.utm_medium, j.utm_campaign, j.ref))
    # CC-184: over a cap the account is still made, just without the email (a resend can come later)
    email = _verification_email(db, user) if emailcaps.may_send(db, user.email, emailcaps.VERIFY, user.id) else None
    out = _issue(db, user, response)  # commits the account and its link together
    if email is not None:
        tasks.add_task(mail.send, email)
    return out


@router.post("/login", response_model=TokenOut, dependencies=[Depends(limit_auth_attempts)])
def login(body: LoginIn, db: DB, response: Response):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not verify_password(body.password, user.password_hash if user else None):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email or password is incorrect")
    return _issue(db, user, response)


@router.post("/refresh", response_model=TokenOut)
def refresh(db: DB, response: Response, cc_refresh: str | None = Cookie(default=None)):
    """Swap the refresh cookie for a new access token and a new refresh cookie (rotation).

    A token stays usable for REUSE_GRACE_SECONDS after it is swapped, so two tabs that
    refresh at the same moment both succeed. Used again after that, it was probably
    copied, so every session for the account is ended.
    """
    if not cc_refresh:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    token = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(cc_refresh))
                      .with_for_update())
    now = datetime.now(UTC)
    max_age = timedelta(days=get_settings().max_session_days)
    # CC-203: a session lasts max_session_days from its sign-in, however often it refreshed
    if token is None or _utc(token.expires_at) <= now or _utc(token.signed_in_at) + max_age <= now:
        _clear_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")
    if token.revoked_at is not None and (now - _utc(token.revoked_at)).total_seconds() > REUSE_GRACE_SECONDS:
        _end_all_sessions(db, db.get(User, token.user_id))
        db.commit()
        _clear_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")
    if token.revoked_at is None:
        token.revoked_at = now
    user = db.get(User, token.user_id)
    return _issue(db, user, response, signed_in_at=_utc(token.signed_in_at))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(db: DB, response: Response, cc_refresh: str | None = Cookie(default=None)):
    """Delete this session's refresh token, and the user's already-swapped ones so none
    of them can be replayed during the grace period."""
    if cc_refresh:
        token = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(cc_refresh)))
        if token is not None:
            db.execute(delete(RefreshToken).where(
                RefreshToken.user_id == token.user_id,
                or_(RefreshToken.id == token.id, RefreshToken.revoked_at.is_not(None))))
            db.commit()
    _clear_cookie(response)


@router.post("/logout-everywhere", status_code=status.HTTP_204_NO_CONTENT)
def logout_everywhere(db: DB, user: CurrentUser, response: Response):
    """CC-203: sign this account out on every device, this one included, at once."""
    _end_all_sessions(db, user)
    db.commit()
    _clear_cookie(response)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@router.patch("/me", response_model=UserOut, dependencies=[Depends(limit_auth_attempts)])
def update_me(body: ProfilePatch, db: DB, user: CurrentUser, tasks: BackgroundTasks):
    """Edit your own profile. All or nothing: if any part is refused, nothing is saved.

    - email is stored lower-case; the same address in different case is not a change
    - a new email needs the right current_password (403 if wrong, so the page doesn't
      mistake it for an expired sign-in) and must not belong to another account (409)
    - a new address takes effect at once but is unconfirmed: a link is emailed to it
    - CC-203: the old address is told of the change, in case it wasn't them
    - other sessions stay signed in; tokens carry the account id, not the email
    - role and id can't be changed here
    """
    if body.display_name is not None and body.display_name.strip() != user.display_name:
        _check_name_free(db, body.display_name, user)  # an existing name can stay as it is
    new_email = body.email is not None and body.email.lower() != user.email
    old_email = user.email
    if new_email:
        if not body.current_password or not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")
        user.email = body.email.lower()
        user.email_verified_at = None
    if body.display_name is not None:
        user.display_name = body.display_name.strip()
    try:
        db.flush()  # an address that belongs to another account fails here
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists")
    email = (_verification_email(db, user)  # also voids links sent to the old address
             if new_email and emailcaps.may_send(db, user.email, emailcaps.VERIFY, user.id) else None)
    notice = (mail.email_changed(old_email)
              if new_email and emailcaps.may_send(db, old_email, emailcaps.NOTICE, user.id) else None)
    db.commit()
    for e in (notice, email):
        if e is not None:
            tasks.add_task(mail.send, e)
    db.refresh(user)
    return user


def _password_notice(db: DB, user: User, tasks: BackgroundTasks, how: str) -> None:
    """CC-203: tell the account its password changed, in case it wasn't them. Queued after the response;
    over a CC-184 cap it's skipped like any other email."""
    if emailcaps.may_send(db, user.email, emailcaps.NOTICE, user.id):
        tasks.add_task(mail.send, mail.password_changed(user, how))


@router.post("/password", response_model=TokenOut, dependencies=[Depends(limit_auth_attempts)])
def change_password(body: PasswordChangeIn, db: DB, user: CurrentUser, response: Response,
                    tasks: BackgroundTasks):
    """Change your password. Every other session is signed out; this one gets a fresh
    access token and refresh cookie, so it stays signed in. The account is emailed a notice (CC-203).

    - wrong current_password: 403 (not 401, so the page doesn't mistake it for an expired sign-in)
    - a new password the same as the current one: 400
    """
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The new password is the same as the current one")
    user.password_hash = hash_password(body.new_password)
    _end_all_sessions(db, user)
    _password_notice(db, user, tasks, "changed")
    return _issue(db, user, response)  # commits the new password and this session's new token together


# ---- email verification and password reset (CC-44) -----------------------------------

@router.post("/verify-email/send", response_model=MessageOut, dependencies=[Depends(limit_auth_attempts)])
def resend_verification(db: DB, user: CurrentUser, tasks: BackgroundTasks):
    """Email a new confirm-your-email link to the signed-in account (earlier links stop working)."""
    if user.email_verified:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your email address is already confirmed")
    if emailcaps.may_send(db, user.email, emailcaps.VERIFY, user.id, resend=True):  # CC-184: else the same reply, no email
        email = _verification_email(db, user)
        db.commit()
        tasks.add_task(mail.send, email)
    return MessageOut(detail=f"We've sent a new link to {user.email}.")


@router.post("/verify-email", response_model=UserOut, dependencies=[Depends(limit_auth_attempts)])
def verify_email(body: TokenIn, db: DB):
    """Confirm an address from the emailed link. No sign-in needed: the link may be opened
    on another device. Links work once, expire, and only for the address they were sent to.
    Opening an already-used link again succeeds while that address is still confirmed, so a
    link that a mail scanner got to first doesn't show the person an error."""
    user = mail.redeem_token(db, body.token, EmailTokenPurpose.verify_email)
    if user is None:
        db.rollback()
        user = mail.already_confirmed_by(db, body.token)
        if user is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link has expired or was already used")
        return user
    user.email_verified_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user


FORGOT_REPLY = "If that address has an account, we've emailed it a link to reset the password."


@router.post("/forgot-password", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(limit_auth_attempts)])
def forgot_password(body: ForgotPasswordIn, db: DB, tasks: BackgroundTasks):
    """Email a password-reset link. Always answers the same way, so it can't be used to find
    out who has an account; the email itself goes out after the response."""
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is not None and emailcaps.may_send(db, user.email, emailcaps.RESET, user.id, resend=True):  # CC-184
        raw = mail.issue_token(db, user, EmailTokenPurpose.reset_password,
                               timedelta(minutes=get_settings().reset_link_minutes))
        db.commit()
        tasks.add_task(mail.send, mail.reset_password(user, raw))
    return MessageOut(detail=FORGOT_REPLY)


@router.post("/reset-password", response_model=TokenOut, dependencies=[Depends(limit_auth_attempts)])
def reset_password(body: ResetPasswordIn, db: DB, response: Response, tasks: BackgroundTasks):
    """Set a new password from the emailed link, then sign in. Every other session is signed
    out, the address counts as confirmed (they've just shown they can read its mail), and
    the account is emailed a notice (CC-203)."""
    user = mail.redeem_token(db, body.token, EmailTokenPurpose.reset_password)
    if user is None:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link has expired or was already used")
    user.password_hash = hash_password(body.new_password)
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)
    _end_all_sessions(db, user)
    _password_notice(db, user, tasks, "reset")
    return _issue(db, user, response)
