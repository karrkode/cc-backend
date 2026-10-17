#!/bin/sh
# Bring the database schema up to date, then start whatever command was given (the API by default).
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  alembic upgrade head
fi

exec "$@"
