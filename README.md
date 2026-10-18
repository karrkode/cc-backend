# Culture Crew API

FastAPI + PostgreSQL backend for chiculturecrew.com: events, venues, accounts, event
submissions with admin review, and each user's "I've been" list.

The site itself (a static page) is a separate repo, **cc-site**
(github.com/karrkode/cultures-crew-app). The two only meet at this API: the page calls
`api.chiculturecrew.com` from the browser. Each repo deploys on its own.

This code started in cc-site's `backend/` folder and was moved here with its history
(`git subtree split`); see the merge commit that brought it in.

## How work flows

Work is planned in Jira (project **CC** at chiculturecrew.atlassian.net), and GitHub for Jira
links code to issues by their key:

- **Branch:** `CC-123-short-description`
- **Commits:** start with the key, e.g. `CC-123: Add publish_at to events`
- **Pull request** into `main`. CI runs the tests on SQLite and Postgres, checks the Terraform
  formatting and validity, and checks the server scripts parse.
- **Merge.** CI builds the arm64 image, pushes it to ECR tagged with the commit, deploys it to
  the server through SSM, and checks `https://api.chiculturecrew.com/health`. The deploy appears
  on the Jira issue under *Deployments*.

The deploy job assumes an AWS role through OIDC; there are no AWS keys in GitHub. Only the
`production` environment may assume it, and only `main` may use that environment.
Infrastructure (`infra/`) is planned and applied by hand; see `infra/README.md`.

## Run it locally

Needs Docker Desktop.

```
docker compose up --build -d                                  # Postgres + API, migrations run on start
docker compose run --rm api python -m scripts.seed            # load seed/events.json (safe to re-run)
```

- API: http://localhost:8000
- Interactive docs (try every endpoint): http://localhost:8000/docs

To run the whole site locally, clone cc-site next to this repo (`../cc-site`) and add the
`web` profile; the page is then on http://localhost:4173:

```
docker compose --profile web up --build -d
FRONTEND_DIR=/path/to/cc-site/public docker compose --profile web up -d   # cc-site somewhere else
```

The page finds the API by itself (`localhost:8000` when opened from localhost,
`api.<its own domain>` anywhere else), so nothing needs configuring.

### Test accounts

The local stack comes with two accounts ready to use, so both sides of a feature can be tried
(for example, submit an event as the user, then review it as the admin):

| Sign in as | Password | Role | Name | Use it for |
|---|---|---|---|---|
| `admin@example.com` | `local-admin-only` | admin | Local Admin | the review queue, publishing now or later, admin-only details |
| `user@example.com` | `local-user-only` | user | Local User | everything a regular visitor can do: submit (pending review), "I've been", profile |

- **Always there:** `scripts/dev_users.py` runs every time the API container starts. It creates
  each account, or puts back its password, role and confirmed email if they were changed (making
  the test user an admin to try something is undone on the next start), so they survive
  database wipes.
- **Local only:** the logins are in `docker-compose.yml` (`DEV_ADMIN_*`, `DEV_USER_*`), which
  production never uses, and the script refuses to run unless `ENV=local`.
- **Both count as confirmed,** since local email is never really sent.
- Any other account you register locally is ordinary data: it lasts until the database is wiped.

**Email locally.** Signing up, changing your email, and "Forgot password?" send emails with
single-use links. The local stack never really sends them: each one is written to the API log,
link included. To open one, copy its link from:

```
docker compose logs api | grep -A12 "Email to"
```

In production the API sends through Amazon SES (`EMAIL_BACKEND=ses`; the SES setup is in
`infra/aws/ses.tf`). With no `EMAIL_BACKEND` set, production skips emails rather than logging
their links. `REQUIRE_VERIFIED_EMAIL=true` makes people confirm their address before submitting
events; it stays off until SES can reach every inbox (production access).

To make your own account an admin instead, register through `/docs` (POST /auth/register), then:

```
docker compose run --rm api python -m scripts.make_admin you@example.com
```

## Keeping up with the Google Sheet

The events still start life in the Culture Crew Google Sheet (shared as "Anyone with the
link can view"). `scripts/sync_sheet.py` reads it and brings the database in line; it is
meant to run **nightly** (scheduled on AWS in the deployment step).

```
docker compose run --rm api python -m scripts.sync_sheet --dry-run   # what would change
docker compose run --rm api python -m scripts.sync_sheet             # apply it
```

What a run does:

- **New rows** become approved events; **changed rows** update their event.
- Events **added through the site** are never touched; only events that came from the
  sheet (they carry the sheet's id in `source_id`) follow it.
- An admin's **rejection stands**: the details still update, the event stays hidden.
- **Nothing is deleted.** A row removed from the sheet makes its event `withdrawn`
  (hidden, but people's "I've been" marks are kept); if the row comes back, so does the event.
- If the sheet **can't be read**, has no header row, or suddenly has **fewer than half** the
  events currently live from it, the run changes nothing and is recorded as failed. That
  is far more likely a Google hiccup or a half-cleared tab than a real mass removal.
- An event is identified by the sheet's **ID column**. A row with a blank ID falls back to an
  id worked out from **title + venue + start date**, so editing one of those makes it a new
  event (the old one is withdrawn). Each run notes how many rows have no ID.
- Every run is logged in `sheet_syncs`; admins can read it at `GET /admin/syncs`. The
  script exits with status 1 on failure, so the scheduler can alert on it.

`scripts/seed.py` uses the same code to load `seed/events.json` into an empty database;
it never withdraws anything.

### The ID column

Fill it in once, and again whenever rows are added without one:

```
.venv/bin/python -m scripts.sheet_ids    # reads the sheet only; needs no database
```

It says whether the sheet has a column headed **ID** yet and which rows are missing one,
then prints a column to paste in, one line per sheet row from the row under the header.
The values are each row's **current** id, the one its event already has, so share links
and "I've been" marks carry over. Keep existing IDs as they are; any unique text up to
40 characters works for new rows (type one in when you add the row, e.g. `ev-0325`). If two
rows share an ID, the first one wins.

Set the column to **Format → Number → Plain text**, or Sheets turns ids like `038656` or
`541e30` into numbers (`38656`, `5.41E+32`). `scripts.sheet_ids` lists any that look
reformatted. The sync copes meanwhile: a row whose ID matches no event, but whose title,
venue and start date match an event no other row claims, keeps that event, and the run's
message says how many rows that happened to.

If you hide the column in Google Sheets, run `scripts.sheet_ids` afterwards and check it
still says "ID column: found"; if hiding removes it from the sheet's public view, the sync
can't see it.

## Tests

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                         # in-memory SQLite, nothing else needed
TEST_DATABASE_URL=postgresql+psycopg://culture:culture@localhost:5432/culture_test .venv/bin/pytest
```

The Postgres run also checks that the Alembic migrations produce exactly the schema
in `app/models.py`. (Create the test database once with
`docker compose exec db psql -U culture -c "create database culture_test"`.)

## Changing the schema

Edit `app/models.py`, then with the database running:

```
.venv/bin/alembic revision --autogenerate -m "what changed"
```

Read the generated file in `alembic/versions/` before committing it. The container
applies new migrations when it starts.

## Endpoints

| | Endpoint | Who |
|---|---|---|
| Auth | `POST /auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`; `GET /auth/me` | anyone / signed in |
| Events | `GET /events` (filters: `q`, `area`, `category`, `from`, `to`, `max_price`, `include_past`, `limit`, `offset`), `GET /events/{id}` | anyone |
| | `POST /events` (always pending for users; admins choose `publish`: `now`, `later` with `publish_at`, or `review`) | signed in |
| | `PATCH /events/{id}`, `DELETE /events/{id}` | admin |
| Review | `GET /admin/events?status=pending`, `GET /admin/events/scheduled`, `POST /admin/events/{id}/approve` (publishes now), `/reject` | admin |
| Venues | `GET /venues`, `POST /venues` | anyone / admin |
| Me | `GET /me/attended`, `PUT`/`DELETE /me/attended/{event_id}`, `GET /me/submissions` | signed in |
| Ops | `GET /health` (API and database) | anyone |

## How sign-in works

- Passwords are hashed with bcrypt; the plain password is never stored or logged.
- Signing in returns a 15-minute **access token** (JWT) in the response body. The page
  keeps it in memory and sends it as `Authorization: Bearer <token>`.
- It also sets a 30-day **refresh token** in an `HttpOnly`, `SameSite=Lax` cookie
  scoped to `/auth`, so page scripts can't read it. Only its SHA-256 hash is stored.
- `POST /auth/refresh` swaps the cookie for a new access token **and a new refresh
  token** (rotation). A swapped token still works for 30 seconds, so two tabs refreshing
  at the same moment both succeed. If it shows up again after that, it was probably
  copied, so every session for that account is ended.
- Signing out deletes the session's refresh token (and its already-swapped ones).
- The page keeps the access token in a JavaScript variable only, never in localStorage,
  and sends refreshes one at a time.
- A user's role is read from the database on each request, so removing an admin takes
  effect immediately rather than when their token expires.
- Login and register are limited to 10 tries a minute per IP. The counter is in memory,
  which is right for one server; several servers would need a shared store such as Redis.

## Layout

```
app/main.py          app, CORS, /health
app/config.py        settings from environment variables
app/models.py        tables (SQLAlchemy)
app/schemas.py       request/response shapes (Pydantic)
app/security.py      bcrypt, JWTs, refresh tokens
app/deps.py          database session, current user, admin check
app/routers/         auth, events, venues, me, admin
alembic/             migrations
app/sheet.py         reads and parses the Google Sheet
app/sync.py          applies the sheet (or the seed file) to the database
scripts/sync_sheet.py  the nightly sync
scripts/sheet_ids.py   the ID column to paste into the sheet
scripts/seed.py      import seed/events.json
seed/events.json     the events from the old Google Sheet site
scripts/make_admin.py
scripts/dev_users.py the local test accounts (ENV=local only)
tests/
```
