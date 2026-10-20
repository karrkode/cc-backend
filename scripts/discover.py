"""Check event sources that are due (CC-70). Found events go to a holding list for review; nothing
reaches the live catalog by itself.

    python -m scripts.discover                 every source that's due, then pre-opening event pages
    python -m scripts.discover --source 3      just that source, if due
    python -m scripts.discover --source 3 --now   that source, now
    ... --key-stdin                            read the Anthropic API key from standard input

On the server the hourly schedule passes the key from Parameter Store through standard input, so it
is never on a command line or in a file. Without a key, only the Art Institute (its own API) and
unchanged pages can be checked; changed pages are reported as needing the key.
"""
import argparse
import sys

from app.db import SessionLocal
from app.discovery.run import run
from app.stats import today_in_chicago


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=int, help="only this source (its id)")
    parser.add_argument("--now", action="store_true", help="check it even if it isn't due")
    parser.add_argument("--key-stdin", action="store_true", help="read the Anthropic API key from standard input")
    args = parser.parse_args()

    client = None
    key = sys.stdin.readline().strip() if args.key_stdin else ""
    if key and key != "unset":
        import anthropic
        client = anthropic.Anthropic(api_key=key, timeout=120.0, max_retries=2)

    with SessionLocal() as db:
        results = run(db, client, today_in_chicago(), only=args.source, force=args.now)
    if not results:
        print("Nothing due.")
    for name, out in results:
        print(f"{name}: {out.status}, {out.new} new, {out.updates} updates, {out.skipped} left out, "
              f"{out.pages_read} pages read ({out.tokens_in}+{out.tokens_out} tokens)" + (f" - {'; '.join(out.notes)}" if out.notes else ""))


if __name__ == "__main__":
    main()
