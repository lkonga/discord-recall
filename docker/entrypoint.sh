#!/usr/bin/env bash
# Container entrypoint: migrate the store, start the scheduler, serve the UI.
set -euo pipefail

cd /app

export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:////data/discord_recall.db}"
mkdir -p /data

echo "[entrypoint] migrations"
python -m alembic upgrade head

if [[ "${RUN_SCHEDULER:-true}" == "true" ]]; then
  echo "[entrypoint] starting scheduler"
  python /app/docker/scheduler.py &
else
  echo "[entrypoint] scheduler disabled (RUN_SCHEDULER=false)"
fi

echo "[entrypoint] serving UI on ${WEB_HOST:-0.0.0.0}:${WEB_PORT:-9879}"
exec uvicorn discord_recall.web.app:app \
  --host "${WEB_HOST:-0.0.0.0}" \
  --port "${WEB_PORT:-9879}" \
  --log-level info
