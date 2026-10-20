"""Email the weekly event-discovery recap (CC-77). Scheduled for Tuesdays at 10 AM Chicago time.

    python -m scripts.recap             build it and send it to settings.recap_to
    python -m scripts.recap --print     just print it

Exits with status 1 when sending fails, so the scheduler's failure alert fires.
"""
import argparse
import sys

from app.db import SessionLocal
from app.discovery.recap import build_recap
from app.mail import send


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="print the email instead of sending it")
    args = parser.parse_args()

    with SessionLocal() as db:
        email = build_recap(db)
    if args.print:
        print(f"To: {email.to}\nSubject: {email.subject}\n\n{email.text}")
        return
    try:
        send(email, strict=True)
    except Exception as e:  # noqa: BLE001 - reported, then the run fails
        print(f"Recap FAILED to send to {email.to}: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Recap sent to {email.to}: {email.subject}")


if __name__ == "__main__":
    main()
