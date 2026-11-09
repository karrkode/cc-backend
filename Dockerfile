FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY seed ./seed
COPY docker-entrypoint.sh .

RUN useradd --system --uid 10001 api && chmod +x docker-entrypoint.sh
USER api

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

ENTRYPOINT ["./docker-entrypoint.sh"]
# --proxy-headers: take the client's address (for rate limits) from X-Forwarded-For, which Caddy sets in
# production. CC-191: only when the request comes from the proxy, i.e. from a Docker network (Docker gives
# them addresses in these two private ranges); anyone else could put any address in that header.
# See infra/README.md before putting a CDN or load balancer in front of Caddy.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--forwarded-allow-ips", "172.16.0.0/12,192.168.0.0/16"]
