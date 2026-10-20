"""Make sure the local test accounts exist, so both sides of the site can be tried straight away.

    python -m scripts.dev_users

| Account                         | Role  | Name        | For                                   |
|---------------------------------|-------|-------------|---------------------------------------|
| DEV_ADMIN_EMAIL / _PASSWORD     | admin | Local Admin | the review queue, publishing, admin   |
| DEV_USER_EMAIL / _PASSWORD      | user  | Local User  | everything a regular visitor can do   |

The admin also has the plan dolphins on (CC-61) and the user doesn't, so both looks can be tried.

Three culture crews (CC-74) come ready-made too, with seven more members who sign in with the test
user's password (DEV_USER_PASSWORD). The admin is in all three and owns one, so both the owner's and a
member's view can be tried; the test user is in none, so joining from an invite link can be tried.

| Crew             | Owner       | Members                                          |
|------------------|-------------|--------------------------------------------------|
| Thursday Theater | Local Admin | Maya Chen, Jordan Reyes, Priya Patel, Marcus Bell |
| The Visual Gaze  | Priya Patel | Local Admin, Lena Fischer, Rosa Alvarez          |
| Laughin Times    | Sam Okafor  | Local Admin, Jordan Reyes, Marcus Bell, Rosa Alvarez |

The members are maya@, jordan@, priya@, sam@, lena@, marcus@ and rosa@example.com. Sam has also invited
the test user to Laughin Times by email (CC-59), so the invite notice on sign-in can be tried; it's put
back until the test user joins. Each crew is made if
it's missing, and anyone listed who isn't in it is put back; members you add yourself are left alone.

The values are set for the local stack in docker-compose.yml (and listed in the README). Each
account is created, or has its password, role and confirmed email put back if they were changed;
running it again changes nothing. The local API container runs this on every start.

Only works with ENV=local, so these accounts can never be created in production.
"""
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Crew, CrewEmailInvite, CrewMember, Role, User
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


# CC-74: the ready-made crews. "admin" is the local admin; the others are crew members made below.
CREW_MEMBERS = {"maya": "Maya Chen", "jordan": "Jordan Reyes", "priya": "Priya Patel", "sam": "Sam Okafor",
                "lena": "Lena Fischer", "marcus": "Marcus Bell", "rosa": "Rosa Alvarez"}
CREWS = [("Thursday Theater", "Plays on Thursday nights, then a drink after", "admin", ["maya", "jordan", "priya", "marcus"]),
         ("The Visual Gaze", "Gallery openings and museum late nights", "priya", ["admin", "lena", "rosa"]),
         ("Laughin Times", "Improv, sketch and stand-up", "sam", ["admin", "jordan", "marcus", "rosa"])]


def ensure_dev_invite(db, user_email: str) -> str:
    """CC-59: Sam has invited the test user to Laughin Times by email, so the sign-in notice can be tried.
    Put back unless the test user has joined (then it's done its job until the next database wipe)."""
    from datetime import timedelta
    crew = db.scalar(select(Crew).where(Crew.name == "Laughin Times"))
    user = db.scalar(select(User).where(User.email == user_email))
    sam = db.scalar(select(User).where(User.email == "sam@example.com"))
    if crew is None or sam is None or (user and any(m.user_id == user.id for m in crew.members)):
        return "Invite for the test user: not needed."
    invite = db.scalar(select(CrewEmailInvite).where(CrewEmailInvite.crew_id == crew.id, CrewEmailInvite.email == user_email))
    if invite is not None and (invite.expires_at if invite.expires_at.tzinfo else invite.expires_at.replace(tzinfo=UTC)) > datetime.now(UTC):
        return "Invite for the test user: unchanged."
    if invite is not None:
        db.delete(invite)
        db.flush()
    db.add(CrewEmailInvite(crew_id=crew.id, email=user_email, invited_by=sam.id, expires_at=datetime.now(UTC) + timedelta(days=14)))
    db.commit()
    return "Invite for the test user: created."


def ensure_dev_crews(db, admin_email: str, password: str) -> list[str]:
    """The ready-made crews and their members. Returns a line per crew saying what happened."""
    people = {"admin": db.scalar(select(User).where(User.email == admin_email))}
    for handle, name in CREW_MEMBERS.items():
        ensure_dev_user(db, f"{handle}@example.com", password, Role.user, name)
        people[handle] = db.scalar(select(User).where(User.email == f"{handle}@example.com"))
    said = []
    for name, about, owner, members in CREWS:
        crew = db.scalar(select(Crew).where(Crew.name == name).order_by(Crew.id))
        made = crew is None
        if made:
            crew = Crew(name=name, description=about, owner_id=people[owner].id)
            db.add(crew)
            db.flush()
        have = {m.user_id for m in crew.members}
        added = [h for h in [owner, *members] if people[h].id not in have]
        for h in added:
            crew.members.append(CrewMember(user_id=people[h].id))
        db.commit()
        said.append(f"Crew {name}: " + ("created" if made else f"{len(added)} put back" if added else "unchanged") + ".")
    return said


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
        if s.dev_admin_email and s.dev_user_password:  # the crews need the admin, and a password for their members
            for line in ensure_dev_crews(db, s.dev_admin_email.strip().lower(), s.dev_user_password):
                print(line)
            if s.dev_user_email:
                print(ensure_dev_invite(db, s.dev_user_email.strip().lower()))


if __name__ == "__main__":
    main()
