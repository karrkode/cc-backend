#!/bin/sh
# Bring the database schema up to date, then start whatever command was given (the API by default).
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  if [ -n "${OWNER_DATABASE_URL:-}" ]; then
    # CC-200: the owner migrates and gives the app role (DATABASE_URL) its password.
    python -m scripts.migrate
  else
    alembic upgrade head  # one database user: tests, CI, an env file from before CC-200
  fi
fi
# What runs next connects only as DATABASE_URL, never as the owner.
unset OWNER_DATABASE_URL

# Local stack only: make sure the test accounts exist (logged to stderr so a script's output stays
# clean). Production never sets DEV_ADMIN_* or DEV_USER_*, and the script itself refuses unless
# ENV=local. A failure here never stops the API.
if [ -n "${DEV_ADMIN_EMAIL:-}${DEV_USER_EMAIL:-}" ]; then
  python -m scripts.dev_users >&2 || echo "warning: could not set up the local test accounts" >&2
fi

exec "$@"
