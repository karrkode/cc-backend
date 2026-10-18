"""Make sure the local development admin exists, so admin pages can be tried straight away.

    python -m scripts.dev_admin

Reads DEV_ADMIN_EMAIL and DEV_ADMIN_PASSWORD (set for the local stack in docker-compose.yml).
Creates the account, or puts its password and admin role back if they were changed; running
it again changes nothing. The local API container runs this on every start.

Only works with ENV=local, so it can never create an account in production.
"""
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Role, User
from app.security import hash_password, verify_password


def ensure_dev_admin(db, email: str, password: str) -> str:
    """Returns what happened: "created", "updated" or "unchanged"."""
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        db.add(User(email=email, password_hash=hash_password(password), display_name="Local Admin", role=Role.admin,
                    email_verified_at=datetime.now(UTC)))
        db.commit()
        return "created"
    changed = False
    if user.email_verified_at is None:  # local mail is never really sent, so count it as confirmed
        user.email_verified_at = datetime.now(UTC)
        changed = True
    if not verify_password(password, user.password_hash):
        user.password_hash = hash_password(password)
        changed = True
    if user.role != Role.admin:
        user.role = Role.admin
        changed = True
    db.commit()
    return "updated" if changed else "unchanged"


def main():
    s = get_settings()
    if s.env != "local":
        raise SystemExit(f"dev_admin only runs with ENV=local (this is ENV={s.env}).")
    if not s.dev_admin_email or not s.dev_admin_password:
        raise SystemExit("Set DEV_ADMIN_EMAIL and DEV_ADMIN_PASSWORD first.")
    with SessionLocal() as db:
        result = ensure_dev_admin(db, s.dev_admin_email, s.dev_admin_password)
    print(f"Local admin {s.dev_admin_email.lower()}: {result}.")


if __name__ == "__main__":
    main()
