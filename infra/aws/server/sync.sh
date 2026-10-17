#!/bin/bash
# culture-crew-sync [--dry-run]
# Run the Google Sheet sync once, in a throwaway container from the deployed image.
# EventBridge Scheduler runs this nightly through SSM; its output goes to CloudWatch, and
# a non-zero exit marks the SSM command failed, which sends an alert.
set -euo pipefail
source /etc/culture-crew/config

TAG=$(cat /etc/culture-crew/deployed-tag 2>/dev/null || echo "$IMAGE_TAG")
/usr/local/bin/culture-crew-env
exec docker run --rm --network cc --env-file /etc/culture-crew/api.env -e RUN_MIGRATIONS=0 \
  "$IMAGE_REPO:$TAG" python -m scripts.sync_sheet "$@"
