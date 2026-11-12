"""Delete expired sessions and used or expired email links (CC-204). Scheduled nightly at 3:30 AM Chicago time.

    python -m scripts.token_cleanup

Exits with status 1 on an error, so the scheduler's failure alert fires.
"""
import sys

from app.db import SessionLocal
from app.housekeeping import clean_tokens


def main():
    try:
        with SessionLocal() as db:
            removed = clean_tokens(db)
    except Exception as e:  # noqa: BLE001 - reported, then the run fails
        print(f"Token clean-up FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Token clean-up removed {removed['sessions']} sessions and {removed['email_links']} email links")


if __name__ == "__main__":
    main()
