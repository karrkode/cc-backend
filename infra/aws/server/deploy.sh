#!/bin/bash
# culture-crew-deploy [TAG]
# Pull the API image from ECR and (re)start the API and Caddy containers.
# Run at boot by culture-crew.service, and by deploys (SSM Run Command) with a new tag.
set -euo pipefail
source /etc/culture-crew/config

TAG="${1:-$(cat /etc/culture-crew/deployed-tag 2>/dev/null || echo "$IMAGE_TAG")}"
IMAGE="$IMAGE_REPO:$TAG"

# No docker login: the ECR credential helper (set up in DOCKER_CONFIG at boot) gets access
# from the instance role for each pull, so no registry token is stored on the server.
docker pull "$IMAGE"
/usr/local/bin/culture-crew-env
# Email is optional for the API: if this fails, emails fail (and are logged) but the site stays up.
mkdir -p /etc/culture-crew/mail-creds
/usr/local/bin/culture-crew-mail-creds || echo "warning: no SES credentials for the API; emails will fail" >&2

docker network inspect cc >/dev/null 2>&1 || docker network create cc

# The API is only on the private Docker network; Caddy is the only way in.
docker rm -f api >/dev/null 2>&1 || true
docker run -d --name api --network cc --restart unless-stopped \
  --env-file /etc/culture-crew/api.env \
  -v /etc/culture-crew/mail-creds:/run/aws:ro \
  -e AWS_SHARED_CREDENTIALS_FILE=/run/aws/credentials -e AWS_EC2_METADATA_DISABLED=true \
  --log-driver awslogs --log-opt awslogs-region="$REGION" \
  --log-opt awslogs-group="$LOG_GROUP" --log-opt awslogs-stream=api \
  "$IMAGE"

# Caddy terminates HTTPS (Let's Encrypt, renewed automatically) and proxies to the API.
# Its certificates live in the caddy_data volume, so restarts don't request new ones.
if ! docker ps --format '{{.Names}}' | grep -qx caddy; then
  docker rm -f caddy >/dev/null 2>&1 || true
  docker run -d --name caddy --network cc --restart unless-stopped \
    -p 80:80 -p 443:443 -p 443:443/udp \
    -v /etc/culture-crew/Caddyfile:/etc/caddy/Caddyfile:ro \
    -v caddy_data:/data -v caddy_config:/config \
    --log-driver awslogs --log-opt awslogs-region="$REGION" \
    --log-opt awslogs-group="$LOG_GROUP" --log-opt awslogs-stream=caddy \
    public.ecr.aws/docker/library/caddy:2.10-alpine
fi

echo "$TAG" > /etc/culture-crew/deployed-tag

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
