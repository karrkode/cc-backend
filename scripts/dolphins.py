"""Turn the plan dolphins on (or off) for an account (CC-61).

    python -m scripts.dolphins you@example.com
    python -m scripts.dolphins you@example.com --off

Only the owner's accounts have the dolphins; everyone else's plans get their own look. Like
make_admin there's no API endpoint for this, and the addresses live only in the database, never
in the (public) code.
"""
import argparse

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("--off", action="store_true", help="take the dolphins away again")
    args = parser.parse_args()

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == args.email.strip().lower()))
        if user is None:
            raise SystemExit(f"No account for {args.email}. Register it first.")
        user.dolphins = not args.off
        db.commit()
        print(f"{user.email}: dolphins {'on' if user.dolphins else 'off'}.")


if __name__ == "__main__":
    main()
