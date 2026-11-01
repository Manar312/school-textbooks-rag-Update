#!/bin/sh
# Role "api": apply database migrations, then start the web server. Any other command (celery ...) runs as given.
set -e
if [ "$1" = "api" ]; then
  # Prometheus multiprocess mode keeps per-worker files here; stale files from a previous run would be double counted.
  rm -rf "${PROMETHEUS_MULTIPROC_DIR:-/tmp/prom}"/* 2>/dev/null || true
  alembic upgrade head
  # --proxy-headers + forwarded-allow-ips: trust nginx's X-Forwarded-For so login throttling sees the real client IP
  # (safe because the api port is NOT published: only nginx can reach it).
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${API_WORKERS:-2}" \
       --proxy-headers --forwarded-allow-ips='*'
fi
exec "$@"
