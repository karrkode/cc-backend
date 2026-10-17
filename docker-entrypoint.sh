#!/bin/sh
# Bring the database schema up to date, then start whatever command was given (the API by default).
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  alembic upgrade head
fi

# Local stack only: make sure the ready-made admin exists (logged to stderr so a script's output stays clean). Production never sets DEV_ADMIN_EMAIL,
# and the script itself refuses unless ENV=local. A failure here never stops the API.
if [ -n "${DEV_ADMIN_EMAIL:-}" ]; then
  python -m scripts.dev_admin >&2 || echo "warning: could not set up the local admin" >&2
fi

exec "$@"
