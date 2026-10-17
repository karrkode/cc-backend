#!/bin/bash
# culture-crew-deploy [TAG]
# Pull the API image from ECR and (re)start the API and Caddy containers.
# Run at boot by culture-crew.service, and by deploys (SSM Run Command) with a new tag.
set -euo pipefail
source /etc/culture-crew/config

TAG="${1:-$(cat /etc/culture-crew/deployed-tag 2>/dev/null || echo "$IMAGE_TAG")}"
IMAGE="$IMAGE_REPO:$TAG"

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${IMAGE_REPO%%/*}"
docker pull "$IMAGE"
/usr/local/bin/culture-crew-env

docker network inspect cc >/dev/null 2>&1 || docker network create cc

# The API is only on the private Docker network; Caddy is the only way in.
docker rm -f api >/dev/null 2>&1 || true
docker run -d --name api --network cc --restart unless-stopped \
  --env-file /etc/culture-crew/api.env \
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
