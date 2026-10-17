from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..deps import DB, CurrentUser
from ..models import RefreshToken, User
from ..ratelimit import limit_auth_attempts
from ..schemas import LoginIn, RegisterIn, TokenOut, UserOut
from ..security import (create_access_token, hash_password, hash_refresh_token, new_refresh_token,
                        verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE = "cc_refresh"
COOKIE_PATH = "/auth"  # the browser only sends it to the auth endpoints
REUSE_GRACE_SECONDS = 30


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite hands back naive datetimes


def _issue(db: DB, user: User, response: Response) -> TokenOut:
    """Make a new access token, and a new refresh token in the cookie."""
    s = get_settings()
    raw = new_refresh_token()
    db.add(RefreshToken(user_id=user.id, token_hash=hash_refresh_token(raw),
                        expires_at=datetime.now(UTC) + timedelta(days=s.refresh_token_days)))
    db.commit()
    response.set_cookie(COOKIE, raw, max_age=s.refresh_token_days * 86400, path=COOKIE_PATH,
                        domain=s.cookie_domain, secure=s.cookie_secure, httponly=True, samesite="lax")
    access, expires_in = create_access_token(user.id, user.role.value)
    return TokenOut(access_token=access, expires_in=expires_in, user=UserOut.model_validate(user))


def _clear_cookie(response: Response):
    s = get_settings()
    response.delete_cookie(COOKIE, path=COOKIE_PATH, domain=s.cookie_domain, secure=s.cookie_secure,
                           httponly=True, samesite="lax")


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(limit_auth_attempts)])
def register(body: RegisterIn, db: DB, response: Response):
    user = User(email=body.email.lower(), password_hash=hash_password(body.password),
                display_name=body.display_name.strip())
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists")
    return _issue(db, user, response)


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
    if token is None or _utc(token.expires_at) <= now:
        _clear_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")
    if token.revoked_at is not None and (now - _utc(token.revoked_at)).total_seconds() > REUSE_GRACE_SECONDS:
        db.execute(delete(RefreshToken).where(RefreshToken.user_id == token.user_id))
        db.commit()
        _clear_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")
    if token.revoked_at is None:
        token.revoked_at = now
    user = db.get(User, token.user_id)
    return _issue(db, user, response)


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


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user
