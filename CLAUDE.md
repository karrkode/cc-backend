# cc-backend

FastAPI + SQLAlchemy 2 + Alembic on Postgres. See README.md (setup, endpoints, local accounts) and
`infra/README.md` (AWS). The site is the sibling repo `../cc-site`.

## Tests

- `.venv/bin/pytest -q` runs on SQLite.
- Run Postgres too before a PR:
  `TEST_DATABASE_URL=postgresql+psycopg://culture:culture@localhost:5432/culture_test .venv/bin/pytest -q`
- Use `app.stats.today_in_chicago()` in tests, never `date.today()`. CI runs in UTC and fails in the Chicago evening.
- Emails land in `app.mail.OUTBOX` in tests (`EMAIL_BACKEND=memory`).

## Local stack

- `docker compose --profile web up --build -d`: the API on :8000, the site on :4173 (served from ../cc-site/public).
- Rebuild after backend changes with `docker compose up -d --build api`. Migrations run on start.
- Scripts in the container: `docker compose exec -w /srv -e PYTHONPATH=/srv api python -m scripts.<name>`.
- Local emails are written to the API log: `docker compose logs api | grep -A16 "Email to"`.

## Production

- EC2 (us-east-2) running Docker (container `api`) and Caddy. Get the instance id with
  `terraform -chdir=infra/aws output -raw api_instance_id`.
- Merging to `main` deploys through GitHub Actions (`.github/workflows/ci-cd.yml`).
- Check after a deploy through SSM (`aws ssm send-command ... AWS-RunShellScript`):
  `docker exec api alembic current`, plus `curl https://api.chiculturecrew.com/health`.
- The container uses short-lived credentials from the email_sender role (`infra/aws/server/mail-creds.sh`). The
  IMDS hop limit is 1, so the container can't use the instance role.
- Don't change `infra/aws/server/write-env.sh` or user_data: either one replaces the server. To change a production
  setting, give it a default that depends on `ENV=prod` in `app/config.py` instead.
- SES is in us-east-1 (`infra/aws/ses.tf`) and has production access. Photos are in S3 with Rekognition in
  us-east-2 (`infra/aws/photos.tf`).

## Conventions

- Path parameters that are UUIDs need the `uuid.UUID` type.
- One migration per story, numbered in sequence in `alembic/versions/`.
- Comments say why, briefly, with the story key (e.g. `# CC-83: ...`).
