"""CC-204: the nightly clean-up of sessions and email links."""
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.housekeeping import clean_tokens
from app.models import EmailToken, EmailTokenPurpose, RefreshToken, Role, User
from scripts import token_cleanup

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def h(name):
    return hashlib.sha256(name.encode()).hexdigest()


@pytest.fixture
def user(db):
    u = User(email="ana@example.com", display_name="Ana", password_hash="x", role=Role.user)
    db.add(u); db.commit()
    return u


def session(user, name, expires, revoked=False, signed_in=None):
    return RefreshToken(user_id=user.id, token_hash=h(name), expires_at=expires,
                        revoked_at=NOW - timedelta(days=1) if revoked else None, signed_in_at=signed_in or NOW)


def link(user, name, purpose, expires, used=False):
    return EmailToken(user_id=user.id, purpose=purpose, token_hash=h(name), email=user.email, expires_at=expires,
                      used_at=NOW - timedelta(hours=1) if used else None)


def test_removes_only_what_can_no_longer_be_used(db, user):
    day = timedelta(days=1)
    V, R = EmailTokenPurpose.verify_email, EmailTokenPurpose.reset_password
    db.add_all([
        session(user, "live", NOW + day),
        session(user, "expired", NOW - day),
        session(user, "too-old", NOW + day, signed_in=NOW - timedelta(days=91)),
        session(user, "revoked-live", NOW + day, revoked=True),  # kept: replaying it is how theft is caught
        session(user, "revoked-expired", NOW - day, revoked=True),
        link(user, "verify-live", V, NOW + day),
        link(user, "verify-used-valid", V, NOW + day, used=True),  # kept: a second open says "already confirmed"
        link(user, "verify-expired", V, NOW - day),
        link(user, "reset-live", R, NOW + day),
        link(user, "reset-used", R, NOW + day, used=True),
        link(user, "reset-expired", R, NOW - day, used=True),
    ])
    db.commit()
    assert clean_tokens(db, NOW) == {"sessions": 3, "email_links": 3}
    assert {t.token_hash for t in db.scalars(select(RefreshToken))} == {h("live"), h("revoked-live")}
    assert {t.token_hash for t in db.scalars(select(EmailToken))} == {h("verify-live"), h("verify-used-valid"), h("reset-live")}
    assert clean_tokens(db, NOW) == {"sessions": 0, "email_links": 0}  # nothing left to do


def test_a_replayed_token_still_ends_every_session_after_cleanup(db, user):
    db.add_all([session(user, "revoked-live", NOW + timedelta(days=1), revoked=True),
                session(user, "other", NOW + timedelta(days=1))])
    db.commit()
    clean_tokens(db, NOW)
    kept = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == h("revoked-live")))
    assert kept is not None and kept.revoked_at is not None


def test_the_script_reports_and_fails_loudly(db, Session, monkeypatch, capsys):
    monkeypatch.setattr(token_cleanup, "SessionLocal", Session)
    token_cleanup.main()
    assert "removed 0 sessions and 0 email links" in capsys.readouterr().out

    def broken(_db):
        raise RuntimeError("database gone")
    monkeypatch.setattr(token_cleanup, "clean_tokens", broken)
    with pytest.raises(SystemExit) as stop:
        token_cleanup.main()
    assert stop.value.code == 1 and "FAILED" in capsys.readouterr().err
