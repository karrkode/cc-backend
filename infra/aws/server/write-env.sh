#!/bin/bash
# culture-crew-env
# Build /etc/culture-crew/api.env from Secrets Manager (database) and Parameter Store (JWT key).
# Run just before a container starts, so a rotated database password is picked up.
# The file is root-only; secrets never touch the image, the repo or Terraform state.
set -euo pipefail
source /etc/culture-crew/config

DB_SECRET=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$DB_SECRET_ARN" \
  --query SecretString --output text)
JWT=$(aws ssm get-parameter --region "$REGION" --name "$JWT_PARAM" --with-decryption \
  --query Parameter.Value --output text)

# The generated password can contain URL-special characters, so quote it properly.
DATABASE_URL=$(DB_SECRET="$DB_SECRET" python3 -c '
import json, os, urllib.parse as u
s = json.loads(os.environ["DB_SECRET"])
print("postgresql+psycopg://%s:%s@%s:5432/%s?sslmode=require" % (
    u.quote(s["username"], safe=""), u.quote(s["password"], safe=""), os.environ["DB_HOST"], os.environ["DB_NAME"]))
')

umask 077
cat > /etc/culture-crew/api.env.tmp <<EOF
ENV=prod
DATABASE_URL=$DATABASE_URL
JWT_SECRET=$JWT
CORS_ORIGINS=$CORS_ORIGINS
COOKIE_SECURE=true
EOF
mv /etc/culture-crew/api.env.tmp /etc/culture-crew/api.env
