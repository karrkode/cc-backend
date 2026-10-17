# Culture Crew API

FastAPI + PostgreSQL backend for chiculturecrew.com: events, venues, accounts, event
submissions with admin review, and each user's "I've been" list.

## Run it locally

Needs Docker Desktop.

```
cd backend
docker compose up --build -d                                  # Postgres + API, migrations run on start
docker compose run --rm api python -m scripts.seed            # load data/events.json (safe to re-run)
```

- API: http://localhost:8000
- Interactive docs (try every endpoint): http://localhost:8000/docs

To make yourself an admin, register through `/docs` (POST /auth/register), then:

```
docker compose run --rm api python -m scripts.make_admin you@example.com
```

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
| | `POST /events` (pending for users, published for admins) | signed in |
| | `PATCH /events/{id}`, `DELETE /events/{id}` | admin |
| Review | `GET /admin/events?status=pending`, `POST /admin/events/{id}/approve`, `/reject` | admin |
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
  token** (rotation). If an already-used refresh token shows up again, it was probably
  copied, so every session for that account is ended.
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
scripts/seed.py      import data/events.json
scripts/make_admin.py
tests/
```
