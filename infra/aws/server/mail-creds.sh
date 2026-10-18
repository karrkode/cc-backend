#!/bin/bash
# culture-crew-mail-creds
# Give the API container short-lived credentials that can only send account email (SES).
# The container can't reach the instance role (IMDS hop limit 1), so the host assumes the
# send-only role and writes its temporary credentials to a file the container mounts read-only.
# Runs before each container start (deploy.sh) and every 30 minutes (culture-crew-mail-creds.timer);
# the credentials last an hour, and the API reads the file fresh for each email it sends.
set -euo pipefail
source /etc/culture-crew/config

DIR=/etc/culture-crew/mail-creds
install -d -m 755 "$DIR"
CREDS=$(aws sts assume-role --region "$REGION" --role-arn "$MAIL_ROLE_ARN" \
  --role-session-name culture-crew-api --duration-seconds 3600 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text) # set -e stops here on failure
read -r KEY SECRET TOKEN <<< "$CREDS"
[ -n "$TOKEN" ] || { echo "assume-role returned no credentials" >&2; exit 1; }

umask 077
printf '[default]\naws_access_key_id = %s\naws_secret_access_key = %s\naws_session_token = %s\n' \
  "$KEY" "$SECRET" "$TOKEN" > "$DIR/credentials.tmp"
chown 10001 "$DIR/credentials.tmp" # the image's "api" user; nothing else on the host reads it
mv "$DIR/credentials.tmp" "$DIR/credentials"
