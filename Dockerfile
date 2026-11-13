# CC-194: the base is pinned by digest (the multi-arch index), so every build starts from the same bytes;
# Dependabot proposes digest bumps. Packages come from requirements.txt, which carries a hash for each one.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS base

# Build stage: packages go into a venv with no pip inside it. --require-hashes refuses anything not in the lock;
# --only-binary means no package runs a setup.py while the image builds.
FROM base AS build
WORKDIR /srv
COPY requirements.txt .
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --no-cache-dir --disable-pip-version-check \
      --require-hashes --only-binary=:all: -r requirements.txt

# Final stage: the base's pip is removed (its files stay in a lower layer, but can't be run from this one).
FROM base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
RUN pip uninstall -y pip && rm -rf /usr/local/lib/python3.*/ensurepip
COPY --from=build /opt/venv /opt/venv

WORKDIR /srv

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
