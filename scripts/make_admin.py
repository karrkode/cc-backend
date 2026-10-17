"""Give an existing account the admin role (or take it away).

    python -m scripts.make_admin you@example.com
    python -m scripts.make_admin you@example.com --revoke

There is deliberately no API endpoint for this: the first admin has to be made by
someone with database access.
"""
import argparse

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Role, User


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("--revoke", action="store_true", help="make the account a regular user again")
    args = parser.parse_args()

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == args.email.lower()))
        if user is None:
            raise SystemExit(f"No account for {args.email}. Register it first (POST /auth/register).")
        user.role = Role.user if args.revoke else Role.admin
        db.commit()
        print(f"{user.email} is now {user.role.value}.")


if __name__ == "__main__":
    main()
