# --- frontend (React + Vite + shadcn/ui) ---
FROM node:22-alpine AS web
WORKDIR /app/web
# The frontend is optional at build time: if web/package.json is absent the
# stage emits an empty dist and the image serves the server-rendered fallback.
COPY web ./
RUN if [ -f package.json ]; then \
      npm install --no-audit --no-fund && npm run build; \
    else \
      mkdir -p dist && echo "no frontend sources present"; \
    fi

# --- backend + UI host ---
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/discord_recall.db \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=9879 \
    WEB_DIST=/app/web/dist

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[web]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY docker ./docker
COPY --from=web /app/web/dist /app/web/dist
RUN chmod +x /app/docker/entrypoint.sh

VOLUME ["/data"]
EXPOSE 9879

HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:9879/healthz" || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
