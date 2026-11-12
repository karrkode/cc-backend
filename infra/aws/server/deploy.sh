#!/bin/bash
# culture-crew-deploy [TAG [CADDY_IMAGE]]
# Pull the API image from ECR and (re)start the API and Caddy containers.
# Run at boot by culture-crew.service, and by deploys (SSM Run Command) with a new tag and
# the Caddy image pinned in infra/aws/server/caddy/Dockerfile.
set -euo pipefail
source /etc/culture-crew/config

# CC-200: no "latest". Without a tag from the deploy, use the one this server last ran, else the one CI recorded
# in Parameter Store (a new or replaced server's first boot).
TAG="${1:-$(cat /etc/culture-crew/deployed-tag 2>/dev/null || aws ssm get-parameter --region "$REGION" \
  --name "/culture-crew/api-image-tag" --query Parameter.Value --output text 2>/dev/null || true)}"
if [ -z "$TAG" ] || [ "$TAG" = "None" ]; then
  echo "No image tag: pass one, or set /culture-crew/api-image-tag in Parameter Store" >&2
  exit 1
fi
IMAGE="$IMAGE_REPO:$TAG"

# CC-183: Caddy is pinned by digest. Deploys pass the current pin, so a Dependabot bump reaches the
# server on the next merge without changing this boot script (which would replace the server).
# The default is only for a new server's first boot, until the next deploy.
CADDY_IMAGE="${2:-$(cat /etc/culture-crew/caddy-image 2>/dev/null || echo \
  public.ecr.aws/docker/library/caddy:2.11.7-alpine@sha256:e70b164da81ef698b3ad19af799ea6086ce6d9e48ed4079eb9fe26f6fbf14ee5)}"
if ! [[ "$CADDY_IMAGE" =~ ^[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]]; then
  echo "Caddy image must be pinned by digest: $CADDY_IMAGE" >&2
  exit 1
fi

# No docker login: the ECR credential helper (set up in DOCKER_CONFIG at boot) gets access
# from the instance role for each pull, so no registry token is stored on the server.
docker pull "$IMAGE"
/usr/local/bin/culture-crew-env
# Email is optional for the API: if this fails, emails fail (and are logged) but the site stays up.
mkdir -p /etc/culture-crew/mail-creds
/usr/local/bin/culture-crew-mail-creds || echo "warning: no SES credentials for the API; emails will fail" >&2

docker network inspect cc >/dev/null 2>&1 || docker network create cc

# CC-200: migrate as the database owner in a one-off container, then start the API as the app role. If it fails,
# the old API keeps serving and this deploy fails (and alerts). api.env and migrate.env share no keys. Needs an
# image from CC-200 or later (scripts.migrate), and migration 0055 already applied: merge and deploy first, then
# replace the server. Keep migrations expand-only: the old API serves while this runs.
docker run --rm --network cc \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges \
  --env-file /etc/culture-crew/api.env --env-file /etc/culture-crew/migrate.env \
  "$IMAGE" true

# The API is only on the private Docker network; Caddy is the only way in.
# CC-183: a read-only filesystem (scratch space in /tmp only), no Linux capabilities, no privilege
# gain, and memory and process limits so one runaway request can't starve the 1 GB server.
docker rm -f api >/dev/null 2>&1 || true
docker run -d --name api --network cc --restart unless-stopped \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 600m --memory-swap 600m --pids-limit 200 \
  --env-file /etc/culture-crew/api.env -e RUN_MIGRATIONS=0 \
  -v /etc/culture-crew/mail-creds:/run/aws:ro \
  -e AWS_SHARED_CREDENTIALS_FILE=/run/aws/credentials -e AWS_EC2_METADATA_DISABLED=true \
  --log-driver awslogs --log-opt awslogs-region="$REGION" \
  --log-opt awslogs-group="$LOG_GROUP" --log-opt awslogs-stream=api \
  "$IMAGE"

# Caddy terminates HTTPS (Let's Encrypt, renewed automatically) and proxies to the API.
# Its certificates live in the caddy_data volume, so restarts don't request new ones.
# CC-183: recreate it only when the pinned image changes (or it isn't running).
if [ "$(docker inspect -f '{{.State.Running}} {{.Config.Image}}' caddy 2>/dev/null)" != "true $CADDY_IMAGE" ]; then
  docker pull "$CADDY_IMAGE"
  docker rm -f caddy >/dev/null 2>&1 || true
  # It only needs to bind ports 80 and 443.
  docker run -d --name caddy --network cc --restart unless-stopped \
    --cap-drop ALL --cap-add NET_BIND_SERVICE --security-opt no-new-privileges \
    --memory 200m --pids-limit 200 \
    -p 80:80 -p 443:443 -p 443:443/udp \
    -v /etc/culture-crew/Caddyfile:/etc/caddy/Caddyfile:ro \
    -v caddy_data:/data -v caddy_config:/config \
    --log-driver awslogs --log-opt awslogs-region="$REGION" \
    --log-opt awslogs-group="$LOG_GROUP" --log-opt awslogs-stream=caddy \
    "$CADDY_IMAGE"
fi

echo "$TAG" > /etc/culture-crew/deployed-tag
echo "$CADDY_IMAGE" > /etc/culture-crew/caddy-image

for _ in $(seq 1 30); do
  if docker exec api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" 2>/dev/null; then
    echo "API is healthy, running $IMAGE"
    docker image prune -af --filter "until=168h" >/dev/null || true
    exit 0
  fi
  sleep 2
done
echo "API did not become healthy:" >&2
docker logs --tail 50 api >&2 || true
exit 1
