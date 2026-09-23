FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/discord_recall.db \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=9879

WORKDIR /app

# Discord needs no build deps for the self-bot client, but keep the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[web]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY docker ./docker
RUN chmod +x /app/docker/entrypoint.sh

VOLUME ["/data"]
EXPOSE 9879

HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:9879/healthz" || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
