#!/bin/sh
# Daily Postgres backup (cron: 0 3 * * * /srv/school-rag/deploy/backup.sh). Qdrant is rebuildable: POST /admin/books/{id}/reindex.
set -e
DIR="${BACKUP_DIR:-/var/backups/school-rag}"; mkdir -p "$DIR"
docker compose exec -T postgres pg_dump -U rag schoolrag | gzip > "$DIR/schoolrag-$(date +%F).sql.gz"
find "$DIR" -name 'schoolrag-*.sql.gz' -mtime +14 -delete
