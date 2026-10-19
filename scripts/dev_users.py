"""Make sure the local test accounts exist, so both sides of the site can be tried straight away.

    python -m scripts.dev_users

| Account                         | Role  | Name        | For                                   |
|---------------------------------|-------|-------------|---------------------------------------|
| DEV_ADMIN_EMAIL / _PASSWORD     | admin | Local Admin | the review queue, publishing, admin   |
| DEV_USER_EMAIL / _PASSWORD      | user  | Local User  | everything a regular visitor can do   |

The admin also has the plan dolphins on (CC-61) and the user doesn't, so both looks can be tried.

The values are set for the local stack in docker-compose.yml (and listed in the README). Each
account is created, or has its password, role and confirmed email put back if they were changed;
running it again changes nothing. The local API container runs this on every start.

Only works with ENV=local, so these accounts can never be created in production.
"""
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Role, User
from app.security import hash_password, verify_password


def ensure_dev_user(db, email: str, password: str, role: Role, display_name: str) -> str:
    """Returns what happened: "created", "updated" or "unchanged"."""
    email = email.strip().lower()
    dolphins = role == Role.admin
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        db.add(User(email=email, password_hash=hash_password(password), display_name=display_name, role=role,
                    dolphins=dolphins, email_verified_at=datetime.now(UTC)))
        db.commit()
        return "created"
    changed = False
    if user.email_verified_at is None:  # local mail is never really sent, so count it as confirmed
        user.email_verified_at = datetime.now(UTC)
        changed = True
    if not verify_password(password, user.password_hash):
        user.password_hash = hash_password(password)
        changed = True
    if user.role != role:  # e.g. the test user was made an admin to try something
        user.role = role
        changed = True
    if user.dolphins != dolphins:
        user.dolphins = dolphins
        changed = True
    db.commit()
    return "updated" if changed else "unchanged"


def accounts(s) -> list[tuple[str, str, Role, str, str]]:
    """The configured accounts as (label, email, role, name, password). A half-set pair is an error."""
    out = []
    for label, email, password, role, name in (("admin", s.dev_admin_email, s.dev_admin_password, Role.admin, "Local Admin"),
                                               ("user", s.dev_user_email, s.dev_user_password, Role.user, "Local User")):
        if bool(email) != bool(password):
            var = f"DEV_{label.upper()}"
            raise SystemExit(f"Set both {var}_EMAIL and {var}_PASSWORD (or neither).")
        if email:
            out.append((label, email.strip().lower(), role, name, password))
    if len({a[1] for a in out}) < len(out):
        raise SystemExit("DEV_ADMIN_EMAIL and DEV_USER_EMAIL must be different addresses.")
    return out


def main():
    s = get_settings()
    if s.env != "local":
        raise SystemExit(f"dev_users only runs with ENV=local (this is ENV={s.env}).")
    wanted = accounts(s)
    if not wanted:
        raise SystemExit("Set DEV_ADMIN_EMAIL/DEV_ADMIN_PASSWORD and/or DEV_USER_EMAIL/DEV_USER_PASSWORD first.")
    with SessionLocal() as db:
        for label, email, role, name, password in wanted:
            print(f"Local {label} {email}: {ensure_dev_user(db, email, password, role, name)}.")


if __name__ == "__main__":
    main()
